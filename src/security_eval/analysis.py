"""Descriptive metrics and deterministic bootstrap intervals for experiment runs."""

import math
import random
from statistics import mean
from typing import Any


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(max(math.ceil(quantile * len(ordered)) - 1, 0), len(ordered) - 1)
    return round(ordered[index], 2)


def bootstrap_mean_ci(
    values: list[bool | float], seed: int, iterations: int = 1000, confidence_level: float = 0.95
) -> dict[str, float] | None:
    if not values:
        return None
    if not 0 < confidence_level < 1 or iterations < 1:
        raise ValueError("Bootstrap confidence level and iteration count are invalid")

    rng = random.Random(seed)
    n = len(values)
    boot_means = sorted(
        mean(rng.choice(values) for _ in range(n)) for _ in range(iterations)
    )
    alpha = (1 - confidence_level) / 2
    low = min(math.floor(alpha * iterations), iterations - 1)
    high = min(math.ceil((1 - alpha) * iterations) - 1, iterations - 1)
    return {
        "low": round(boot_means[low], 4),
        "high": round(boot_means[high], 4),
    }


def summarize_results(
    results: list[dict[str, Any]],
    arms: list[str],
    *,
    seed: int,
    iterations: int = 1000,
    confidence_level: float = 0.95,
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for arm_index, arm in enumerate(arms):
        arm_results = [row for row in results if row["arm"] == arm]
        successful = [row for row in arm_results if not row.get("error")]
        attack_results = [row for row in successful if row["kind"] == "attack"]
        benign_results = [row for row in successful if row["kind"] == "benign"]

        attack_outcomes = [bool(row.get("attack_success")) for row in attack_results]
        by_category: dict[str, list[bool]] = {}
        for row in attack_results:
            by_category.setdefault(str(row["category"]), []).append(bool(row.get("attack_success")))
        false_refusals = [bool(row.get("false_refusal")) for row in benign_results]
        benign_successes = [bool(row.get("benign_task_success")) for row in benign_results]
        latencies = [float(row["latency_ms"]) for row in successful]
        guardrail_latencies = [float(row["guardrail_latency_ms"]) for row in successful]

        summaries[arm] = {
            "total": len(arm_results),
            "completed": len(successful),
            "errors": len(arm_results) - len(successful),
            "attack_count": len(attack_results),
            "attack_successes": sum(attack_outcomes),
            "attack_success_rate": round(mean(attack_outcomes), 4) if attack_outcomes else None,
            "attack_success_ci95": bootstrap_mean_ci(
                attack_outcomes, seed + arm_index, iterations, confidence_level
            ),
            "attack_by_category": {
                category: {
                    "count": len(outcomes),
                    "successes": sum(outcomes),
                    "rate": round(mean(outcomes), 4),
                    "ci95": bootstrap_mean_ci(
                        outcomes,
                        seed + 400 + arm_index + category_index,
                        iterations,
                        confidence_level,
                    ),
                }
                for category_index, (category, outcomes) in enumerate(sorted(by_category.items()))
            },
            "benign_count": len(benign_results),
            "false_refusals": sum(false_refusals),
            "false_refusal_rate": round(mean(false_refusals), 4) if false_refusals else None,
            "false_refusal_ci95": bootstrap_mean_ci(
                false_refusals, seed + 100 + arm_index, iterations, confidence_level
            ),
            "benign_task_success_rate": (
                round(mean(benign_successes), 4) if benign_successes else None
            ),
            "guardrail_blocked": sum(bool(row.get("guardrail_blocked")) for row in arm_results),
            "p50_latency_ms": percentile(latencies, 0.50),
            "p95_latency_ms": percentile(latencies, 0.95),
            "p50_guardrail_latency_ms": percentile(guardrail_latencies, 0.50),
        }
    baseline = [row for row in results if row["arm"] == "off" and not row.get("error")]
    baseline_by_kind: dict[str, dict[str, dict[str, bool]]] = {"attack": {}, "benign": {}}
    for row in baseline:
        kind = row["kind"]
        metric = "attack_success" if kind == "attack" else "false_refusal"
        value = row.get(metric)
        if value is not None:
            baseline_by_kind[kind][row["case_id"]] = {metric: bool(value)}

    for arm_index, arm in enumerate(arms):
        if arm == "off" or arm not in summaries:
            continue
        treatment = [
            row for row in results if row["arm"] == arm and not row.get("error")
        ]
        paired: dict[str, list[float]] = {"attack": [], "benign": []}
        for row in treatment:
            kind = row["kind"]
            metric = "attack_success" if kind == "attack" else "false_refusal"
            before = baseline_by_kind[kind].get(row["case_id"])
            after = row.get(metric)
            if before is not None and after is not None:
                paired[kind].append(float(bool(after)) - float(before[metric]))

        summaries[arm]["paired_vs_off"] = {
            "attack_cases": len(paired["attack"]),
            "attack_success_delta": round(mean(paired["attack"]), 4)
            if paired["attack"]
            else None,
            "attack_success_delta_ci95": bootstrap_mean_ci(
                paired["attack"], seed + 200 + arm_index, iterations, confidence_level
            ),
            "benign_cases": len(paired["benign"]),
            "false_refusal_delta": round(mean(paired["benign"]), 4)
            if paired["benign"]
            else None,
            "false_refusal_delta_ci95": bootstrap_mean_ci(
                paired["benign"], seed + 300 + arm_index, iterations, confidence_level
            ),
        }

    return summaries
