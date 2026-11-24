"""Provider-free integration tests for the Phase 2 additions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_app import main
from llm_app.schemas import ChatRequest, ChatResponse, ChatUsage, GuardrailVerdict
from llm_app.security import TokenBucketLimiter
from llm_app.trace_store import TraceStore
from security_eval.dataset import load_dataset
from security_eval.runner import EvaluationRunManager, resolve_request_budget

CANARY = "IDT-CANARY-7K2Q-2026"


async def _fake_chat(request: ChatRequest, session_id: str) -> ChatResponse:
    return ChatResponse(
        answer=f"Answer containing {CANARY} for this evaluation case.",
        model="fake-model",
        latency_ms=12,
        usage=ChatUsage(prompt_tokens=5, completion_tokens=6, total_tokens=11),
        guardrail=GuardrailVerdict(level="off", outcome="passed", blocked=False, latency_ms=0),
        trace_id="trace-from-fake-model",
    )


class _Settings:
    evaluation_default_max_requests = 60
    evaluation_hard_max_requests = 300
    prefilter_block_threshold = 0.80
    prefilter_allow_threshold = 0.20
    adaptive_enabled = True


class _UnsetEnvDefaultSettings:
    """Mimics an environment where EVALUATION_DEFAULT_MAX_REQUESTS is unset."""

    evaluation_default_max_requests = None
    evaluation_hard_max_requests = 300
    prefilter_block_threshold = 0.80
    prefilter_allow_threshold = 0.20
    adaptive_enabled = True


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    class Backend:
        provider = "ollama"
        model = "test-model"

        async def chat(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
            return {
                "content": "A harmless answer.",
                "prompt_tokens": 4,
                "completion_tokens": 3,
            }

        async def is_available(self) -> bool:
            return True

        async def list_models(self) -> list[str]:
            return ["test-model", "alt-model"]

    class Guardrails:
        async def inspect_input(self, messages: list[dict[str, str]], level: str) -> Any:
            from llm_app.guardrails import GuardrailResult

            return GuardrailResult(
                messages=messages.copy(), findings=[], outcome="passed", latency_ms=0
            )

        async def inspect_output(self, prompt: str, output: str, level: str, *args: Any) -> Any:
            from llm_app.guardrails import GuardrailResult

            return GuardrailResult(messages=[], findings=[], outcome="passed", latency_ms=0)

        async def classifier_is_configured(self) -> bool:
            return False

    monkeypatch.setattr(main, "TRACE_STORE", TraceStore(tmp_path / "traces"))
    monkeypatch.setattr(main, "RATE_LIMITER", TokenBucketLimiter(per_minute=1000))
    monkeypatch.setattr(main.settings, "api_auth_token", "")
    app = main.app
    app.dependency_overrides[main.get_backend] = lambda: Backend()
    app.dependency_overrides[main.get_guardrail_pipeline] = lambda: Guardrails()
    app.dependency_overrides[main.get_evaluation_manager] = lambda: EvaluationRunManager(
        tmp_path / "results"
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_dev_dataset_is_pinned_and_distinct_from_reported_corpus() -> None:
    dev = load_dataset(Path("data/evaluation/dev-v1.manifest.json"))
    reported = load_dataset(Path("data/evaluation/manifest.json"))
    manifest = json.loads(Path("data/evaluation/dev-v1.manifest.json").read_text())

    assert dev.sha256 == manifest["sha256"]
    assert len(dev.cases) == 20
    assert sum(case.kind == "attack" for case in dev.cases) == 12
    assert sum(case.kind == "benign" for case in dev.cases) == 8
    assert not {case.id for case in dev.cases} & {case.id for case in reported.cases}
    assert "calibration" in manifest["provenance"].lower()


def test_budget_precedence_and_hard_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("security_eval.runner.get_settings", lambda: _Settings())

    assert resolve_request_budget({}, None) == (60, 300)
    assert resolve_request_budget({"default_max_requests": 20}, None) == (60, 300)
    assert resolve_request_budget({}, 10) == (10, 300)
    assert resolve_request_budget({}, 999) == (300, 300)


def test_config_default_is_used_when_env_override_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "security_eval.runner.get_settings", lambda: _UnsetEnvDefaultSettings()
    )

    # The committed config supplies the default when the env var is absent.
    assert resolve_request_budget({"default_max_requests": 20}, None) == (20, 300)
    assert resolve_request_budget({}, None) == (60, 300)
    # An explicit per-run override still wins over the config default.
    assert resolve_request_budget({"default_max_requests": 20}, 10) == (10, 300)
    # The hard ceiling still caps a config-supplied default.
    assert resolve_request_budget({"default_max_requests": 5000}, None) == (300, 300)


def test_reported_run_rejects_dev_dataset_and_unknown_arms(tmp_path: Path) -> None:
    config = json.loads(Path("experiments/config.json").read_text())
    config["dataset_manifest"] = "data/evaluation/dev-v1.manifest.json"
    config_path = tmp_path / "dev-config.json"
    config_path.write_text(json.dumps(config))
    manager = EvaluationRunManager(tmp_path / "results", config_path)
    kwargs = {
        "arms": ["off"],
        "seed": 42,
        "sample_limit": 2,
        "generation_model": "fake-model",
        "generation_backend": "fake",
        "guardrail_backend": "cloudflare",
        "call_chat": _fake_chat,
    }
    with pytest.raises(ValueError, match="dev calibration set"):
        asyncio.run(manager.start_run(**kwargs))

    with pytest.raises(ValueError, match="Unsupported guardrail arms"):
        asyncio.run(
            EvaluationRunManager(tmp_path / "results").start_run(
                **{**kwargs, "arms": ["turbo"]}
            )
        )


def test_run_metadata_records_calibration_provenance(tmp_path: Path) -> None:
    manager = EvaluationRunManager(tmp_path / "results")

    async def run_once() -> dict[str, object]:
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=1,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=_fake_chat,
        )
        assert manager._task is not None
        await manager._task
        return manager.store.get_run(str(run["id"])) or {}

    stored = asyncio.run(run_once())
    expected = json.loads(Path("data/evaluation/dev-v1.manifest.json").read_text())["sha256"]

    assert stored["status"] == "completed"
    assert stored["dev_dataset_sha256"] == expected
    assert stored["prefilter_block_threshold"] == 0.80
    assert stored["prefilter_allow_threshold"] == 0.20
    assert stored["multiplicity_family"] == "non_baseline_arms_x_asr_false_refusal"
    assert stored["planned_requests"] == 1


def test_evaluation_api_rejects_over_ceiling_and_planned_count(client: TestClient) -> None:
    payload = {"arms": ["off"], "sample_limit": 2}

    over_ceiling = client.post(
        "/api/evaluations/runs", json={**payload, "max_requests": 1000}
    )
    assert over_ceiling.status_code == 400
    assert "hard ceiling" in over_ceiling.json()["detail"]

    too_many = client.post(
        "/api/evaluations/runs",
        json={"arms": ["off", "prefilter", "scanners"], "sample_limit": 42},
    )
    assert too_many.status_code == 400
    assert "exceeds the effective cap" in too_many.json()["detail"]


def test_chat_returns_trace_id_and_pricing(client: TestClient) -> None:
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"]
    assert body["cost"] is not None
    assert body["cost"]["priced"] is False  # test-model is not in the pricing catalog

    traces = client.get("/api/traces")
    assert traces.status_code == 200
    record = next(t for t in traces.json()["traces"] if t["trace_id"] == body["trace_id"])
    assert record["kind"] == "chat"
    assert record["arm"] == "off"
    assert client.get(f"/api/traces/{body['trace_id']}").status_code == 200
    assert client.get("/api/traces/does-not-exist").status_code == 404


def test_observability_and_pricing_endpoints(client: TestClient) -> None:
    config = client.get("/api/observability/config").json()
    assert config["phoenix_url"] == "http://localhost:6006"
    assert config["content_capture_enabled"] is False
    assert config["prefilter_block_threshold"] == 0.80
    assert config["prefilter_allow_threshold"] == 0.20
    assert "alert_rules" in config

    pricing = client.get("/api/pricing")
    assert pricing.status_code == 200
    assert pricing.json()["pricing_sha256"]


def test_report_endpoints_exclude_content_by_default(client: TestClient, tmp_path: Path) -> None:
    manager = EvaluationRunManager(tmp_path / "results")

    async def run_once() -> str:
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=1,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=_fake_chat,
        )
        assert manager._task is not None
        await manager._task
        return str(run["id"])

    run_id = asyncio.run(run_once())
    default_html = client.get(f"/api/evaluations/runs/{run_id}/report.html")
    default_json = client.get(f"/api/evaluations/runs/{run_id}/report.json")
    opt_in_json = client.get(f"/api/evaluations/runs/{run_id}/report.json?include_content=true")

    assert default_html.status_code == 200
    assert CANARY not in default_html.text
    assert CANARY not in default_json.text
    assert opt_in_json.status_code == 200
    assert CANARY in opt_in_json.text
    assert client.get(f"/api/evaluations/runs/{run_id}/export.csv").status_code == 200


def test_compare_is_exploratory_and_never_creates_a_run(client: TestClient) -> None:
    before = client.get("/api/evaluations/runs").json()["runs"]
    response = client.post(
        "/api/compare",
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "variants": [
                {"model": "test-model", "guardrail_level": "off"},
                {"model": "alt-model", "guardrail_level": "scanners"},
            ],
        },
    )

    assert response.status_code == 200
    variants = response.json()["variants"]
    assert [variant["model"] for variant in variants] == ["test-model", "alt-model"]
    assert client.get("/api/evaluations/runs").json()["runs"] == before


def test_hardening_headers_body_cap_and_rate_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.get("/api/observability/config")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Referrer-Policy" in response.headers

    too_large = client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "x" * 2000}]},
        headers={"Content-Length": "2000000"},
    )
    assert too_large.status_code == 413

    monkeypatch.setattr(main, "RATE_LIMITER", TokenBucketLimiter(per_minute=1, burst=1))
    assert client.get("/api/observability/config").status_code == 200
    limited = client.get("/api/observability/config")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_retry_aware_budget_aborts_before_next_request(tmp_path: Path) -> None:
    """The budget counts actual attempts, and no call is made after the cap."""
    calls: list[str] = []

    async def retrying_chat(request: ChatRequest, session_id: str) -> ChatResponse:
        calls.append(session_id)
        return ChatResponse(
            answer="Answer containing the canary.",
            model="fake-model",
            latency_ms=5,
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            guardrail=GuardrailVerdict(level="off", outcome="passed", blocked=False, latency_ms=0),
            trace_id="trace-retry",
            attempts=3,
        )

    manager = EvaluationRunManager(tmp_path / "results")

    async def run_once() -> dict[str, object]:
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=4,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=retrying_chat,
            max_requests=5,
        )
        assert manager._task is not None
        await manager._task
        return manager.store.get_run(str(run["id"])) or {}

    stored = asyncio.run(run_once())

    # Each logical request used 3 attempts: after two calls usage is 6 > cap 5,
    # so the runner stops before a third call. Overshoot within the final request
    # is unavoidable because attempts are only known after the call completes.
    assert len(calls) == 2
    assert stored["status"] == "aborted_budget"
    assert stored["partial"] is True
    assert stored["requests_used"] == 6
    assert stored["results_written"] == 2
    assert stored["summary"]["_meta"]["partial"] is True
    assert stored["summary"]["_meta"]["completed"] == 6
    assert stored["error"].startswith("budget_exhausted_after_")


def test_run_id_traversal_is_rejected_without_reading_files(client: TestClient) -> None:
    response = client.get("/api/evaluations/runs/..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in {400, 404}
    assert "root:" not in response.text


def test_compare_endpoints_exclude_partial_runs_by_default(
    client: TestClient, tmp_path: Path
) -> None:
    from security_eval.store import EvaluationStore

    store = EvaluationStore(tmp_path / "partial-runs")
    store.create_run(
        "aaaaaaaa-1111-2222-3333-444444444444",
        {"status": "aborted_budget", "partial": True},
    )
    store.create_run(
        "bbbbbbbb-1111-2222-3333-444444444444",
        {"status": "completed", "partial": False},
    )
    manager = EvaluationRunManager(tmp_path / "partial-runs")
    app = main.app
    app.dependency_overrides[main.get_evaluation_manager] = lambda: manager

    ids = ["aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"]
    query = "&".join(f"run_ids={run_id}" for run_id in ids)
    default = client.get(f"/api/evaluations/compare?{query}").json()
    included = client.get(f"/api/evaluations/compare?{query}&include_partial=true").json()

    assert len(default["runs"]) == 1
    assert default["runs"][0]["status"] == "completed"
    assert len(included["runs"]) == 2
    assert client.get(f"/api/reports/compare.html?{query}").status_code == 200


def test_streamed_body_over_limit_is_rejected_without_content_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunked body with no Content-Length must still hit the size cap."""
    monkeypatch.setattr(main.settings, "max_body_bytes", 1024)

    def chunks():
        yield b'{"messages": [{"role": "user", "content": "'
        for _ in range(20):
            yield b"x" * 100
        yield b'"}]}'

    response = client.post(
        "/api/chat",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "too large" in response.text.lower()


def test_comparison_report_lists_partial_runs_in_the_appendix(
    client: TestClient, tmp_path: Path
) -> None:
    from security_eval.store import EvaluationStore

    store = EvaluationStore(tmp_path / "compare-runs")
    store.create_run(
        "aaaaaaaa-1111-2222-3333-444444444444",
        {
            "id": "aaaaaaaa-1111-2222-3333-444444444444",
            "status": "completed",
            "partial": False,
            "arms": ["off"],
            "summary": {},
        },
    )
    store.create_run(
        "bbbbbbbb-1111-2222-3333-444444444444",
        {
            "id": "bbbbbbbb-1111-2222-3333-444444444444",
            "status": "aborted_budget",
            "partial": True,
            "arms": ["off"],
            "results_written": 3,
            "planned_requests": 42,
            "summary": {},
        },
    )
    manager = EvaluationRunManager(tmp_path / "compare-runs")
    main.app.dependency_overrides[main.get_evaluation_manager] = lambda: manager

    query = (
        "run_ids=aaaaaaaa-1111-2222-3333-444444444444"
        "&run_ids=bbbbbbbb-1111-2222-3333-444444444444"
    )
    html = client.get(f"/api/reports/compare.html?{query}").text

    assert "Appendix" in html
    assert "bbbbbbbb-1111-2222-3333-444444444444" in html
    payload = client.get(f"/api/evaluations/compare?{query}").json()
    assert len(payload["runs"]) == 1
    assert payload["excluded_partial_runs"][0]["id"] == "bbbbbbbb-1111-2222-3333-444444444444"


def test_generation_concurrency_is_bounded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent chat requests must not exceed GENERATION_CONCURRENCY provider calls."""
    import threading

    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowBackend:
        provider = "ollama"
        model = "test-model"

        async def chat(self, messages: Sequence[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                await asyncio.sleep(0.05)
                return {"content": "ok", "prompt_tokens": 1, "completion_tokens": 1}
            finally:
                with lock:
                    active -= 1

        async def is_available(self) -> bool:
            return True

        async def list_models(self) -> list[str]:
            return ["test-model"]

    main.app.dependency_overrides[main.get_backend] = lambda: SlowBackend()
    monkeypatch.setattr(main.settings, "generation_concurrency", 2)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                client.post,
                "/api/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            for _ in range(8)
        ]
        responses = [future.result() for future in futures]

    assert all(response.status_code == 200 for response in responses)
    assert peak <= 2, f"expected at most 2 concurrent generations, saw {peak}"
