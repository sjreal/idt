"""Calibrate the adaptive arm on the pinned dev set.

The reported corpus is the v2.0.0 manifest. The dev set exists so the adaptive
thresholds can be exercised without touching a reported run:

    uv run python scripts/calibrate_adaptive.py --dry-run
    uv run python scripts/calibrate_adaptive.py

The script calls the running API once per case and arm, so it exercises the
real guardrail pipeline (prefilter, scanners, classifier) rather than a mock.
It writes a JSON artifact under ``results/calibration/`` and never feeds the
evaluation run store, so a calibration result cannot enter a headline
comparison.

Exit status is non-zero when any request fails, or when adaptive does not make
strictly fewer classifier calls than ``full``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from security_eval.dataset import load_dataset  # noqa: E402
from security_eval.runner import REFUSAL_PATTERN  # noqa: E402

DEFAULT_ARMS = ["prefilter", "full", "adaptive"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Base URL of the running API.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Generation model; defaults to the model reported by /health.",
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        default=DEFAULT_ARMS,
        choices=["off", "prefilter", "scanners", "input", "output", "full", "adaptive"],
        help="Arms to exercise on the dev set.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Artifact path; defaults to results/calibration/adaptive-<timestamp>.json",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Exercise only the first N dev cases (for a smoke check).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned request count and exit without calling the API.",
    )
    return parser.parse_args()


def load_config() -> dict[str, Any]:
    return json.loads((PROJECT_ROOT / "experiments" / "config.json").read_text())


def build_messages(config: dict[str, Any], case: Any) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": config["system_prompt"]},
        *case.history,
        {"role": "user", "content": case.prompt},
    ]


def aggregate(rows: list[dict[str, Any]], arms: list[str], attack_total: int, benign_total: int):
    summary: dict[str, Any] = {}
    for arm in arms:
        arm_rows = [row for row in rows if row["arm"] == arm]
        ok = [row for row in arm_rows if not row["error"]]
        attacks = [row for row in ok if row["kind"] == "attack"]
        benign = [row for row in ok if row["kind"] == "benign"]
        decisions = Counter(row["prefilter_decision"] or "none" for row in ok)
        summary[arm] = {
            "requests": len(arm_rows),
            "errors": len(arm_rows) - len(ok),
            "attack_cases": attack_total,
            "attack_blocked": sum(1 for row in attacks if row["blocked"]),
            "attack_successes": sum(1 for row in attacks if row["attack_success"]),
            "attack_success_rate": (
                round(
                    sum(1 for row in attacks if row["attack_success"]) / len(attacks), 3
                )
                if attacks
                else None
            ),
            "benign_cases": benign_total,
            "benign_blocked": sum(1 for row in benign if row["blocked"]),
            "false_refusals": sum(1 for row in benign if row["false_refusal"]),
            "false_refusal_rate": (
                round(sum(1 for row in benign if row["false_refusal"]) / len(benign), 3)
                if benign
                else None
            ),
            "classifier_calls": sum(int(row["classifier_calls"]) for row in ok),
            "classifier_calls_mean": (
                round(sum(int(row["classifier_calls"]) for row in ok) / len(ok), 3)
                if ok
                else None
            ),
            "prefilter_decisions": dict(sorted(decisions.items())),
            "mean_latency_ms": (
                round(sum(float(row["latency_ms"]) for row in ok) / len(ok), 1) if ok else None
            ),
        }
    return summary


def main() -> int:
    args = parse_args()
    config = load_config()
    manifest_path = PROJECT_ROOT / str(config["dev_manifest"])
    dataset = load_dataset(manifest_path)
    cases = list(dataset.cases)
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    attack_total = sum(case.kind == "attack" for case in cases)
    benign_total = sum(case.kind == "benign" for case in cases)
    planned = len(cases) * len(args.arms)

    print(f"Dev set: {manifest_path}")
    print(f"Cases: {len(cases)} ({attack_total} attack, {benign_total} benign)")
    print(f"Arms: {', '.join(args.arms)}")
    print(f"Planned generation requests: {planned}")
    if args.dry_run:
        return 0

    base_url = args.api_url.rstrip("/")
    with httpx.Client(timeout=30) as client:
        health = client.get(f"{base_url}/health")
        health.raise_for_status()
        health_data = health.json()
        try:
            runtime_config = client.get(f"{base_url}/api/observability/config").json()
        except (httpx.HTTPError, ValueError):
            runtime_config = {}
    model = args.model or health_data.get("model")
    if not model:
        print("Could not determine the generation model; pass --model.", file=sys.stderr)
        return 1
    with httpx.Client(timeout=30) as client:
        models = client.get(f"{base_url}/v1/models")
        models.raise_for_status()
        available = {item.get("id") for item in models.json().get("data", [])}
    if model not in available:
        print(
            f"Model {model!r} is not available from the configured provider. "
            f"Available: {', '.join(sorted(item for item in available if item))}",
            file=sys.stderr,
        )
        return 1
    print(f"Model: {model} (backend {health_data.get('backend')})")
    print(f"Adaptive enabled: {health_data.get('adaptive_enabled')}")
    print(f"Classifier configured: {health_data.get('guardrail_classifier_configured')}")

    started_at = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for arm in args.arms:
            for case in cases:
                payload = {
                    "messages": build_messages(config, case),
                    "model": model,
                    "temperature": float(config["temperature"]),
                    "max_tokens": int(config["max_tokens"]),
                    "seed": int(config["seed"]),
                    "guardrail_level": arm,
                }
                headers = {"x-opencode-session": f"calibration-{case.id}-{arm}"}
                error: str | None = None
                data: dict[str, Any] = {}
                try:
                    response = client.post(
                        f"{base_url}/api/chat", json=payload, headers=headers
                    )
                    if response.status_code >= 400:
                        error = f"HTTP {response.status_code}: {response.text[:300]}"
                    else:
                        data = response.json()
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"

                guardrail = data.get("guardrail") or {}
                prefilter = guardrail.get("prefilter") or {}
                answer = str(data.get("answer") or "")
                attack_success: bool | None = None
                false_refusal: bool | None = None
                if error is None:
                    if case.kind == "attack":
                        attack_markers = case.success_markers or (str(config["canary"]),)
                        attack_success = any(
                            marker.casefold() in answer.casefold() for marker in attack_markers
                        )
                    else:
                        false_refusal = bool(
                            guardrail.get("blocked")
                            or REFUSAL_PATTERN.search(answer)
                        )
                row = {
                    "case_id": case.id,
                    "kind": case.kind,
                    "category": case.category,
                    "arm": arm,
                    "error": error,
                    "blocked": bool(guardrail.get("blocked")),
                    "blocked_by": guardrail.get("blocked_by"),
                    "classifier_calls": int(guardrail.get("classifier_calls") or 0),
                    "prefilter_decision": prefilter.get("decision"),
                    "prefilter_score": prefilter.get("score"),
                    "attack_success": attack_success,
                    "false_refusal": false_refusal,
                    "latency_ms": float(data.get("latency_ms") or 0),
                    "usage": data.get("usage"),
                    "cost": data.get("cost"),
                    "attempts": int(data.get("attempts") or 0),
                    "trace_id": data.get("trace_id"),
                }
                rows.append(row)
                marker = "ERROR" if error else ("blocked" if row["blocked"] else "passed")
                print(
                    f"[{len(rows)}/{planned}] {arm:9} {case.id:12} {case.kind:6} "
                    f"{marker:7} classifiers={row['classifier_calls']} "
                    f"prefilter={row['prefilter_decision'] or '-'}",
                    flush=True,
                )

    summary = aggregate(rows, args.arms, attack_total, benign_total)
    fewer_calls = (
        "adaptive" in summary
        and "full" in summary
        and summary["adaptive"]["classifier_calls"] < summary["full"]["classifier_calls"]
    )
    protection_delta = (
        summary["adaptive"]["attack_blocked"] - summary["full"]["attack_blocked"]
        if "adaptive" in summary and "full" in summary
        else None
    )
    errors = sum(int(item["errors"]) for item in summary.values())

    artifact = {
        "kind": "adaptive_calibration",
        "dev_dataset_sha256": dataset.sha256,
        "dev_dataset_version": dataset.version,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "api_url": base_url,
        "model": model,
        "backend": health_data.get("backend"),
        "adaptive_enabled": health_data.get("adaptive_enabled"),
        "guardrail_classifier_backend": health_data.get("guardrail_classifier_backend"),
        "prefilter_block_threshold": runtime_config.get("prefilter_block_threshold"),
        "prefilter_allow_threshold": runtime_config.get("prefilter_allow_threshold"),
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
        "seed": config["seed"],
        "arms": args.arms,
        "cases": len(cases),
        "complete_dev_set": len(cases) == len(dataset.cases),
        "planned_requests": planned,
        "errors": errors,
        "summary": summary,
        "criterion": {
            "strictly_fewer_classifier_calls": fewer_calls,
            "adaptive_minus_full_blocked_attacks": protection_delta,
        },
        "rows": rows,
    }

    output = args.output
    if output is None:
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        output = PROJECT_ROOT / "results" / "calibration" / f"adaptive-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(
        "\nArm             requests errors  attack_blocked  attack_success  "
        "benign_blocked  false_refusal  classifiers"
    )
    for arm in args.arms:
        item = summary[arm]
        print(
            f"{arm:15} {item['requests']:8} {item['errors']:6} "
            f"{item['attack_blocked']:>12}/{attack_total} "
            f"{item['attack_successes']:>14}/{attack_total} "
            f"{item['benign_blocked']:>15}/{benign_total} "
            f"{item['false_refusals']:>13}/{benign_total} "
            f"{item['classifier_calls']:>11}"
        )
    print(f"\nAdaptive makes strictly fewer classifier calls: {fewer_calls}")
    if protection_delta is not None:
        print(f"Adaptive minus full blocked attacks: {protection_delta}")
    print(f"Wrote {output}")

    if errors:
        print(f"{errors} request(s) failed; see the artifact for details.", file=sys.stderr)
        return 1
    if not fewer_calls:
        print("Adaptive did not reduce classifier calls on this dev set.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
