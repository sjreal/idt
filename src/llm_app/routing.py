"""Ordered model fallback and per-model circuit breaking for generation providers.

Fallback is opt-in per call: evaluation runs pass ``allow_fallback=False`` so a
provider failure is surfaced instead of silently swapping the model under test.
Only understood transient failures (HTTP 408/425/429/5xx, timeouts, network
errors) qualify for fallback; content-policy refusals and invalid requests never do.
"""

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

T = TypeVar("T")

_RETRYABLE_STATUSES = frozenset({408, 425, 429})
_HTTP_STATUS_RE = re.compile(r"http\s*(\d{3})", re.IGNORECASE)


class RetryableError(Exception):
    """A transient provider failure that may be retried on another model."""


class NonRetryableError(Exception):
    """A permanent failure (auth, invalid request, content policy) that must propagate."""


class FallbackExhausted(RetryableError):
    """Every model in the chain was attempted but none produced a result."""


def is_retryable_status(status: int) -> bool:
    """Return True for HTTP statuses representing transient provider failures."""
    return status in _RETRYABLE_STATUSES or status >= 500


def classify_error(exc: BaseException) -> str:
    """Classify ``exc`` as ``"retryable"``, ``"non_retryable"`` or ``"unknown"``.

    Unknown exceptions are intentionally not treated as retryable, since fallback
    must only happen for failures we understand to be transient.
    """
    if isinstance(exc, RetryableError):
        return "retryable"
    if isinstance(exc, NonRetryableError):
        return "non_retryable"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if is_retryable_status(status):
            return "retryable"
        return "non_retryable" if 400 <= status < 500 else "unknown"
    if isinstance(
        exc,
        (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError),
    ):
        return "retryable"
    return "unknown"


def bounded_reason(exc: BaseException) -> str:
    """Return a small, fixed label describing why a fallback happened.

    The raw exception message is unbounded (it embeds model ids, attempt counts
    and provider text), so it must never be used as a metric label. The full
    text stays in the trace record; only this coarse reason is counted.

    The label set is finite: the exact status for 5xx responses (a fixed range
    of 100 values), a single ``http_4xx`` bucket, the well-known retryable
    statuses, and a handful of transport-level reasons.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return _status_label(exc.response.status_code)
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return "network"
    if isinstance(exc, TimeoutError):
        return "timeout"
    # Provider errors are wrapped in RetryableError/NonRetryableError before
    # they reach the router, so the status is recovered from the message.
    match = _HTTP_STATUS_RE.search(str(exc))
    if match is not None:
        return _status_label(int(match.group(1)))
    name = type(exc).__name__.lower()
    return name[:32] if name else "unknown"


def _status_label(status: int) -> str:
    """Map an HTTP status onto a bounded metric label."""
    if status in _RETRYABLE_STATUSES:
        return f"http_{status}"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return f"http_{status}"
    return "http_other"


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    """Track consecutive failures per model with a cooldown before a trial retry."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._states: dict[str, _CircuitState] = {}

    def state(self, model: str) -> str:
        """Return ``"closed"``, ``"open"`` or ``"half-open"`` for ``model``.

        A circuit is half-open once its cooldown has elapsed, allowing exactly
        one trial attempt before it is closed again or re-opened.
        """
        entry = self._states.get(model)
        if entry is None or entry.opened_at is None:
            return "closed"
        if time.monotonic() - entry.opened_at >= self.cooldown_seconds:
            return "half-open"
        return "open"

    def is_open(self, model: str) -> bool:
        """Return True while ``model`` should be skipped because its circuit is open."""
        return self.state(model) == "open"

    def record_success(self, model: str) -> None:
        """Clear failure history for ``model`` after a successful attempt."""
        self._states.pop(model, None)

    def record_failure(self, model: str) -> None:
        """Record a failure, opening or re-opening the circuit when appropriate."""
        entry = self._states.setdefault(model, _CircuitState())
        if self.state(model) != "closed":
            # A failed half-open trial re-opens the circuit for a fresh cooldown.
            entry.opened_at = time.monotonic()
            entry.failures = self.failure_threshold
            return
        entry.failures += 1
        if entry.failures >= self.failure_threshold:
            entry.opened_at = time.monotonic()

    def reset(self, model: str | None = None) -> None:
        """Forget failure history for one model, or for all models when ``None``."""
        if model is None:
            self._states.clear()
        else:
            self._states.pop(model, None)


@dataclass(frozen=True)
class RoutingDecision:
    """Describe which model answered and how fallback was used."""

    model: str
    attempted_models: list[str]
    fallback_from: str | None
    reason: str | None
    fallback_used: bool
    reason_code: str | None = None


class ModelRouter:
    """Try models in order, falling back only on understood transient errors."""

    def __init__(self, chain: list[str], circuit: CircuitBreaker | None = None) -> None:
        self._chain = tuple(chain)
        self._circuit = circuit if circuit is not None else CircuitBreaker()

    @property
    def chain(self) -> tuple[str, ...]:
        """Return the ordered model chain as an immutable tuple."""
        return self._chain

    @property
    def circuit(self) -> CircuitBreaker:
        """Return the circuit breaker shared by this router's models."""
        return self._circuit

    def validate_chain(self, available_models: list[str]) -> None:
        """Raise ``ValueError`` if the chain is empty or contains unavailable models."""
        if not self._chain:
            raise ValueError("Model chain must not be empty.")
        available = set(available_models)
        unknown = [model for model in self._chain if model not in available]
        if unknown:
            raise ValueError(
                f"Model chain contains unavailable models: {', '.join(unknown)}. "
                f"Available models: {', '.join(sorted(available))}."
            )

    async def run(
        self,
        call: Callable[[str], Awaitable[T]],
        *,
        allow_fallback: bool = True,
    ) -> tuple[T, RoutingDecision]:
        """Attempt each model in order until one succeeds.

        ``call(model)`` performs a single generation attempt. Retryable failures
        advance to the next model only when ``allow_fallback`` is true; all other
        errors (including unknown ones) propagate immediately.
        """
        attempted: list[str] = []
        reasons: list[str] = []
        reason_code: str | None = None
        fallback_from: str | None = None
        fallback_used = False
        last_error: Exception | None = None

        for model in self._chain:
            if self._circuit.is_open(model):
                reasons.append(f"model '{model}' skipped: circuit open")
                continue
            attempted.append(model)
            try:
                result = await call(model)
            except Exception as exc:
                if classify_error(exc) != "retryable":
                    raise
                self._circuit.record_failure(model)
                last_error = exc
                reasons.append(f"model '{model}' failed: {type(exc).__name__}: {exc}")
                if not allow_fallback:
                    raise
                if fallback_from is None:
                    fallback_from = model
                    # The bounded code is the metric label; the raw text above is
                    # only kept for logs and traces.
                    reason_code = bounded_reason(exc)
                fallback_used = True
                continue

            self._circuit.record_success(model)
            return result, RoutingDecision(
                model=model,
                attempted_models=list(attempted),
                fallback_from=fallback_from,
                reason=reasons[-1] if reasons else None,
                fallback_used=fallback_used,
                reason_code=reason_code,
            )

        detail = f"All models failed (attempted: {', '.join(attempted) or 'none'})."
        if reasons:
            detail += " " + "; ".join(reasons)
        raise FallbackExhausted(detail) from last_error
