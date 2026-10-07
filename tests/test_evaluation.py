import asyncio
from pathlib import Path

from llm_app.schemas import ChatRequest, ChatResponse, ChatUsage, GuardrailVerdict
from security_eval.analysis import bootstrap_mean_ci, summarize_results
from security_eval.dataset import load_dataset
from security_eval.runner import EvaluationRunManager


def test_dataset_manifest_matches_fixed_prompt_snapshot() -> None:
    dataset = load_dataset(Path("data/evaluation/manifest.json"))

    assert dataset.version == "2.0.0"
    assert len(dataset.cases) == 42
    assert sum(case.kind == "attack" for case in dataset.cases) == 22
    assert sum(case.kind == "benign" for case in dataset.cases) == 20
    email_case = next(case for case in dataset.cases if case.id == "atk-014")
    assert email_case.success_markers == ("casey.example@example.test",)
    multi_turn_case = next(case for case in dataset.cases if case.id == "atk-021")
    assert len(multi_turn_case.history) == 2


def test_bootstrap_interval_is_deterministic_and_bounds_binary_mean() -> None:
    values = [True, False, False, True, False]

    interval = bootstrap_mean_ci(values, seed=42, iterations=200)

    assert interval is not None
    assert 0 <= interval["low"] <= interval["high"] <= 1
    assert interval == bootstrap_mean_ci(values, seed=42, iterations=200)


def test_summary_reports_asr_false_refusal_and_latency() -> None:
    results = [
        {
            "arm": "off",
            "case_id": "atk-1",
            "kind": "attack",
            "category": "prompt_injection",
            "attack_success": True,
            "false_refusal": None,
            "benign_task_success": None,
            "guardrail_blocked": False,
            "latency_ms": 100,
            "guardrail_latency_ms": 0,
        },
        {
            "arm": "off",
            "case_id": "ben-1",
            "kind": "benign",
            "category": "general",
            "attack_success": None,
            "false_refusal": False,
            "benign_task_success": True,
            "guardrail_blocked": False,
            "latency_ms": 300,
            "guardrail_latency_ms": 0,
        },
        {
            "arm": "scanners",
            "case_id": "atk-1",
            "kind": "attack",
            "category": "prompt_injection",
            "attack_success": False,
            "false_refusal": None,
            "benign_task_success": False,
            "guardrail_blocked": True,
            "latency_ms": 120,
            "guardrail_latency_ms": 20,
        },
        {
            "arm": "scanners",
            "case_id": "ben-1",
            "kind": "benign",
            "category": "general",
            "attack_success": None,
            "false_refusal": True,
            "benign_task_success": False,
            "guardrail_blocked": True,
            "latency_ms": 320,
            "guardrail_latency_ms": 22,
        },
    ]

    summaries = summarize_results(results, ["off", "scanners"], seed=7, iterations=100)
    summary = summaries["off"]

    assert summary["attack_success_rate"] == 1
    assert summary["false_refusal_rate"] == 0
    assert summary["benign_task_success_rate"] == 1
    assert summary["p50_latency_ms"] == 100
    assert summary["p95_latency_ms"] == 300
    assert summary["attack_by_category"]["prompt_injection"]["rate"] == 1
    assert summaries["scanners"]["paired_vs_off"]["attack_success_delta"] == -1
    assert summaries["scanners"]["paired_vs_off"]["false_refusal_delta"] == 1


def test_background_run_persists_run_metadata_and_jsonl_results(tmp_path: Path) -> None:
    async def fake_chat(request: ChatRequest, session_id: str) -> ChatResponse:
        assert session_id.startswith("evaluation-")
        return ChatResponse(
            answer=(
                "IDT-CANARY-7K2Q-2026 Casey Example casey.example@example.test "
                "+1-202-555-0147 DEMO-PII-91F2"
            ),
            model="fake-model",
            latency_ms=42,
            usage=ChatUsage(prompt_tokens=8, completion_tokens=9, total_tokens=17),
            guardrail=GuardrailVerdict(
                level="off",
                outcome="passed",
                blocked=False,
                latency_ms=0,
            ),
            sanitized_user_message=request.messages[-1].content,
        )

    async def run_once() -> dict[str, object]:
        manager = EvaluationRunManager(tmp_path)
        run = await manager.start_run(
            arms=["off"],
            seed=42,
            sample_limit=42,
            generation_model="fake-model",
            generation_backend="fake",
            guardrail_backend="cloudflare",
            call_chat=fake_chat,
        )
        assert manager._task is not None
        await manager._task
        stored = manager.store.get_run(str(run["id"]))
        assert stored is not None
        return stored

    stored_run = asyncio.run(run_once())

    assert stored_run["status"] == "completed"
    assert stored_run["dataset_sha256"] == load_dataset(
        Path("data/evaluation/manifest.json")
    ).sha256
    assert stored_run["results_written"] == 42
    assert len(stored_run["results"]) == 42
    assert stored_run["summary"]["off"]["attack_success_rate"] == 1
