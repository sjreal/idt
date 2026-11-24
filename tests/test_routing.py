"""Tests for model fallback and circuit breaking; no live provider calls are made."""

import asyncio
import time
from typing import Any

import httpx
import pytest

from llm_app.routing import (
    CircuitBreaker,
    FallbackExhausted,
    ModelRouter,
    NonRetryableError,
    RetryableError,
    RoutingDecision,
    bounded_reason,
    classify_error,
    is_retryable_status,
)

MODELS = ["model-a", "model-b", "model-c"]


def status_error(status: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError as if a provider response failed status checks."""
    request = httpx.Request("POST", "https://opencode.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.parametrize(
    "status",
    [408, 425, 429, 500, 502, 503, 504, 599],
)
def test_retryable_http_statuses_are_recognized(status: int) -> None:
    assert is_retryable_status(status) is True
    assert classify_error(status_error(status)) == "retryable"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_non_retryable(status: int) -> None:
    assert is_retryable_status(status) is False
    assert classify_error(status_error(status)) == "non_retryable"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("timed out"), "retryable"),
        (httpx.ConnectTimeout("timed out"), "retryable"),
        (httpx.ConnectError("offline"), "retryable"),
        (TimeoutError("timed out"), "retryable"),
        (ConnectionError("offline"), "retryable"),
        (RetryableError("transient"), "retryable"),
        (NonRetryableError("content policy refusal"), "non_retryable"),
        (ValueError("unexpected"), "unknown"),
    ],
)
def test_classify_error_recognizes_exception_types(error: Exception, expected: str) -> None:
    assert classify_error(error) == expected


def test_unrecognized_http_status_is_unknown() -> None:
    assert classify_error(status_error(302)) == "unknown"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (status_error(502), "http_502"),
        (status_error(429), "http_429"),
        (status_error(500), "http_500"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionError("offline"), "network"),
        (RetryableError("transient"), "retryableerror"),
    ],
    ids=["502", "429", "500", "httpx-timeout", "timeout", "connection", "retryable"],
)
def test_fallback_on_retryable_errors(error: Exception, expected_code: str) -> None:
    attempted: list[str] = []

    async def call(model: str) -> str:
        attempted.append(model)
        if len(attempted) == 1:
            raise error
        return f"answer from {model}"

    result, decision = asyncio.run(ModelRouter(MODELS).run(call))

    assert result == "answer from model-b"
    assert decision == RoutingDecision(
        model="model-b",
        attempted_models=["model-a", "model-b"],
        fallback_from="model-a",
        reason=decision.reason,
        fallback_used=True,
        reason_code=expected_code,
    )
    assert decision.reason and "model-a" in decision.reason


def test_reason_code_is_bounded_and_never_the_raw_message() -> None:
    """The metric label must stay low-cardinality; the raw text stays in traces."""
    error = RetryableError(
        "model 'glm-5.3' failed: OpenCode Go returned HTTP 503 after 3 attempts."
    )

    async def call(model: str) -> str:
        raise error

    with pytest.raises(RetryableError):
        asyncio.run(ModelRouter(MODELS).run(call, allow_fallback=False))

    assert bounded_reason(error) == "http_503"
    assert bounded_reason(status_error(404)) == "http_4xx"
    assert bounded_reason(httpx.ReadTimeout("timed out")) == "timeout"
    assert bounded_reason(ConnectionError("offline")) == "network"


@pytest.mark.parametrize(
    "error",
    [
        status_error(400),
        status_error(401),
        NonRetryableError("content policy refusal"),
    ],
    ids=["400", "401", "content-policy"],
)
def test_non_retryable_errors_propagate_without_fallback(error: Exception) -> None:
    attempted: list[str] = []
    router = ModelRouter(MODELS, CircuitBreaker(failure_threshold=1))

    async def call(model: str) -> str:
        attempted.append(model)
        raise error

    with pytest.raises(type(error)):
        asyncio.run(router.run(call))

    assert attempted == ["model-a"]
    assert router.circuit.state("model-a") == "closed"


def test_unknown_errors_propagate_without_fallback() -> None:
    attempted: list[str] = []

    async def call(model: str) -> str:
        attempted.append(model)
        raise ValueError("unexpected bug")

    with pytest.raises(ValueError, match="unexpected bug"):
        asyncio.run(ModelRouter(MODELS).run(call))

    assert attempted == ["model-a"]


def test_allow_fallback_false_reraises_without_trying_next_model() -> None:
    attempted: list[str] = []

    async def call(model: str) -> str:
        attempted.append(model)
        raise status_error(502)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ModelRouter(MODELS).run(call, allow_fallback=False))

    assert attempted == ["model-a"]


def test_first_model_success_has_no_fallback_metadata() -> None:
    async def call(model: str) -> str:
        return f"answer from {model}"

    result, decision = asyncio.run(ModelRouter(MODELS).run(call))

    assert result == "answer from model-a"
    assert decision == RoutingDecision(
        model="model-a",
        attempted_models=["model-a"],
        fallback_from=None,
        reason=None,
        fallback_used=False,
    )


def test_fallback_exhausted_summarizes_all_attempts() -> None:
    attempted: list[str] = []

    async def call(model: str) -> str:
        attempted.append(model)
        raise status_error(503)

    with pytest.raises(FallbackExhausted) as exc_info:
        asyncio.run(ModelRouter(MODELS).run(call))

    assert attempted == MODELS
    assert "All models failed" in str(exc_info.value)
    assert all(model in str(exc_info.value) for model in MODELS)
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


def test_fallback_exhausted_when_all_circuits_are_open() -> None:
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    for model in MODELS:
        circuit.record_failure(model)
    called: list[str] = []

    async def call(model: str) -> str:
        called.append(model)
        return "unexpected"

    with pytest.raises(FallbackExhausted, match="circuit open"):
        asyncio.run(ModelRouter(MODELS, circuit).run(call))

    assert called == []


def test_circuit_opens_after_threshold_and_closes_after_cooldown(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    circuit = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)

    assert circuit.state("model-a") == "closed"
    circuit.record_failure("model-a")
    assert circuit.is_open("model-a") is False
    circuit.record_failure("model-a")
    assert circuit.is_open("model-a") is True
    assert circuit.state("model-a") == "open"

    now[0] += 9.5
    assert circuit.is_open("model-a") is True
    now[0] += 0.5
    assert circuit.is_open("model-a") is False
    assert circuit.state("model-a") == "half-open"

    circuit.record_success("model-a")
    assert circuit.state("model-a") == "closed"


def test_router_skips_open_circuit_and_uses_half_open_trial(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=5)
    circuit.record_failure("model-a")
    router = ModelRouter(["model-a", "model-b"], circuit)
    attempted: list[str] = []

    async def call(model: str) -> str:
        attempted.append(model)
        return f"answer from {model}"

    result, decision = asyncio.run(router.run(call))

    assert result == "answer from model-b"
    assert attempted == ["model-b"]
    assert decision.attempted_models == ["model-b"]
    assert decision.reason and "circuit open" in decision.reason
    assert circuit.state("model-a") == "open"

    now[0] = 5.0
    assert circuit.state("model-a") == "half-open"
    attempted.clear()
    result, decision = asyncio.run(router.run(call))

    assert result == "answer from model-a"
    assert attempted == ["model-a"]
    assert decision.fallback_used is False
    assert circuit.state("model-a") == "closed"


def test_failed_half_open_trial_reopens_circuit(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=5)
    circuit.record_failure("model-a")
    now[0] = 5.0
    assert circuit.state("model-a") == "half-open"

    circuit.record_failure("model-a")

    assert circuit.state("model-a") == "open"
    now[0] = 9.9
    assert circuit.is_open("model-a") is True
    now[0] = 10.0
    assert circuit.state("model-a") == "half-open"


def test_reset_clears_one_model_or_all_models(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    circuit = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    circuit.record_failure("model-a")
    circuit.record_failure("model-b")

    circuit.reset("model-a")
    assert circuit.state("model-a") == "closed"
    assert circuit.is_open("model-b") is True

    circuit.reset()
    assert circuit.state("model-b") == "closed"


def test_validate_chain_rejects_empty_and_unknown_models() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ModelRouter([]).validate_chain(MODELS)

    with pytest.raises(ValueError, match="unavailable models: missing"):
        ModelRouter(["model-a", "missing"]).validate_chain(MODELS)

    ModelRouter(["model-b", "model-a"]).validate_chain(MODELS)


def test_chain_property_returns_immutable_copy() -> None:
    chain = ["model-a", "model-b"]
    router = ModelRouter(chain)
    chain.append("model-c")

    assert router.chain == ("model-a", "model-b")
    assert isinstance(router.circuit, CircuitBreaker)


def test_failure_does_not_record_circuit_for_unexpected_exception() -> None:
    circuit = CircuitBreaker(failure_threshold=1)

    async def call(model: str) -> Any:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        asyncio.run(ModelRouter(MODELS, circuit).run(call))

    assert circuit.state("model-a") == "closed"
