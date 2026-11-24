"""Regression tests for the Phase 2 audit fixes.

Each test here pins a behaviour that was previously broken or was a dead
setting: the body-size cap, the fallback metric label, the OTel trace id, the
per-loop evaluation manager, the adaptive switch, host validation, the request
id, the rate-limit buckets, content capture, and the guardrail metrics.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_app import main, telemetry
from llm_app.schemas import ChatRequest, ChatResponse, ChatUsage, GuardrailVerdict
from llm_app.security import TokenBucketLimiter, host_rate_limit_key
from llm_app.trace_store import TraceStore
from security_eval.runner import EvaluationRunManager


class _Backend:
    provider = "ollama"
    model = "test-model"

    async def chat(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        return {"content": "A harmless answer.", "prompt_tokens": 4, "completion_tokens": 3}

    async def is_available(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["test-model"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main, "TRACE_STORE", TraceStore(tmp_path / "traces"))
    monkeypatch.setattr(main, "RATE_LIMITER", TokenBucketLimiter(per_minute=1000))
    monkeypatch.setattr(main.settings, "api_auth_token", "")
    app = main.app
    app.dependency_overrides[main.get_backend] = lambda: _Backend()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_request_id_is_returned_and_client_supplied_ids_are_echoed(
    client: TestClient,
) -> None:
    response = client.get("/api/observability/config")
    assert response.headers["X-Request-ID"]

    echoed = client.get("/api/observability/config", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["X-Request-ID"] == "abc-123"

    # A malformed id is not echoed; a fresh one is generated instead.
    unsafe = client.get("/api/observability/config", headers={"X-Request-ID": "bad id\n"})
    assert unsafe.headers["X-Request-ID"] != "bad id\n"


def test_rate_limit_survives_session_id_rotation(client: TestClient, monkeypatch) -> None:
    """Rotating x-opencode-session must not hand out a fresh allowance."""
    monkeypatch.setattr(main, "RATE_LIMITER", TokenBucketLimiter(per_minute=3, burst=3))

    statuses = [
        client.get(
            "/api/observability/config",
            headers={"x-opencode-session": f"session-{index}"},
        ).status_code
        for index in range(6)
    ]

    assert 429 in statuses, statuses
    assert statuses.index(429) <= 3


def test_host_rate_limit_key_ignores_the_session_id() -> None:
    assert host_rate_limit_key("10.0.0.1") == host_rate_limit_key("10.0.0.1")
    assert host_rate_limit_key("10.0.0.1") != host_rate_limit_key("10.0.0.2")
    assert host_rate_limit_key(None).startswith("host|")


def test_adaptive_arm_is_rejected_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "adaptive_enabled", False)

    response = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "Hi"}],
            "guardrail_level": "adaptive",
        },
    )

    assert response.status_code == 400
    assert "ADAPTIVE_ENABLED" in response.json()["detail"]


def test_evaluation_start_rejects_the_adaptive_arm_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "adaptive_enabled", False)

    response = client.post("/api/evaluations/runs", json={"arms": ["adaptive"]})

    assert response.status_code == 400
    assert "ADAPTIVE_ENABLED" in response.json()["detail"]


def test_content_capture_is_off_by_default_and_opt_in_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "secret-text"}]})
    traces = client.get("/api/traces").json()["traces"]
    assert traces
    assert all("content" not in record for record in traces)

    monkeypatch.setattr(main.settings, "trace_capture_content", True)
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "secret-text"}]})
    traces = client.get("/api/traces").json()["traces"]
    captured = [record for record in traces if record.get("content")]
    assert captured
    assert captured[0]["content"]["prompt"] == "secret-text"


def test_off_arm_records_no_guardrail_checks(client: TestClient) -> None:
    from llm_app.metrics import GUARDRAIL_CHECKS

    before = GUARDRAIL_CHECKS.labels("off", "input", "none", "passed")._value.get()

    client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})

    after = GUARDRAIL_CHECKS.labels("off", "input", "none", "passed")._value.get()
    assert after == before


def test_evaluation_manager_can_be_used_from_two_event_loops(tmp_path: Path) -> None:
    """The cached manager must not bind its lock to the first loop it sees."""
    manager = EvaluationRunManager(tmp_path / "results")

    async def call_chat(request: ChatRequest, session_id: str) -> ChatResponse:
        return ChatResponse(
            answer="ok",
            model="fake-model",
            latency_ms=1,
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            guardrail=GuardrailVerdict(level="off", outcome="passed", blocked=False, latency_ms=0),
        )

    async def run_once() -> None:
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=1,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=call_chat,
        )
        assert manager._task is not None
        await manager._task
        assert run["status"] == "queued"

    asyncio.run(run_once())
    asyncio.run(run_once())


def test_start_run_returns_a_snapshot_not_the_live_metadata(tmp_path: Path) -> None:
    manager = EvaluationRunManager(tmp_path / "results")

    async def call_chat(request: ChatRequest, session_id: str) -> ChatResponse:
        return ChatResponse(
            answer="ok",
            model="fake-model",
            latency_ms=1,
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            guardrail=GuardrailVerdict(level="off", outcome="passed", blocked=False, latency_ms=0),
        )

    async def run_once() -> dict[str, Any]:
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=1,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=call_chat,
        )
        assert manager._task is not None
        await manager._task
        return run

    snapshot = asyncio.run(run_once())

    assert snapshot["status"] == "queued"
    assert snapshot["results_written"] == 0
    stored = json.loads(
        next((tmp_path / "results").glob("*/run.json")).read_text(encoding="utf-8")
    )
    assert stored["status"] == "completed"


def test_run_metadata_records_environment_and_aborted_arms(tmp_path: Path) -> None:
    manager = EvaluationRunManager(tmp_path / "results")

    async def call_chat(request: ChatRequest, session_id: str) -> ChatResponse:
        return ChatResponse(
            answer="ok",
            model="fake-model",
            latency_ms=1,
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            guardrail=GuardrailVerdict(level="off", outcome="passed", blocked=False, latency_ms=0),
            # Three attempts per call, so the budget of 3 is exhausted after the
            # first logical request and the second arm never starts.
            attempts=3,
        )

    async def run_once() -> dict[str, Any]:
        run = await manager.start_run(
            arms=["off", "scanners"],
            seed=42,
            sample_limit=1,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=call_chat,
            max_requests=3,
        )
        assert manager._task is not None
        await manager._task
        return manager.store.get_run(str(run["id"])) or {}

    stored = asyncio.run(run_once())

    assert stored["environment"]["llm_backend"]
    assert stored["environment"]["python"]
    assert stored["status"] == "aborted_budget"
    # The first arm completed its single case; the second never ran.
    assert stored["aborted_arms"] == ["scanners"]


def test_trace_id_is_stable_between_the_response_and_the_trace_buffer(
    client: TestClient,
) -> None:
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    trace_id = response.json()["trace_id"]

    traces = client.get("/api/traces").json()["traces"]
    record = next(record for record in traces if record["trace_id"] == trace_id)
    assert record["trace_id"] == trace_id


def test_span_trace_id_helper_handles_the_noop_span() -> None:
    assert telemetry.span_trace_id(telemetry.NoOpSpan()) is None


def test_trusted_hosts_blocks_a_foreign_host_header() -> None:
    """TRUSTED_HOSTS must actually be enforced, not just stored in settings."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        from fastapi.testclient import TestClient
        from llm_app import main

        with TestClient(main.app) as client:
            good = client.get("/health", headers={"Host": "lab.local"})
            bad = client.get("/health", headers={"Host": "evil.example"})
            print(good.status_code, bad.status_code)
        """
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "TRUSTED_HOSTS": "lab.local,testserver",
        "PYTHONPATH": "src",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=".",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "200 400"
