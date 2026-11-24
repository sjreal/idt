"""Descriptive metrics, intervals, and paired hypothesis tests for experiment runs."""

import math
import random
from statistics import NormalDist, mean
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


def _z_value(confidence_level: float) -> float:
    if not 0 < confidence_level < 1:
        raise ValueError("Confidence level must be between 0 and 1")
    return NormalDist().inv_cdf(1 - (1 - confidence_level) / 2)


def wilson_ci(
    successes: int, total: int, confidence_level: float = 0.95
) -> dict[str, float] | None:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it stays inside [0, 1] and behaves
    sensibly with the small samples this project uses.
    """
    if total <= 0:
        return None
    if successes < 0 or successes > total:
        raise ValueError("Successes must be between zero and total")
    z = _z_value(confidence_level)
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return {
        "low": round(max(0.0, centre - margin), 4),
        "high": round(min(1.0, centre + margin), 4),
    }


def mcnemar_exact(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Exact two-sided McNemar test for paired binary outcomes.

    ``pairs`` holds ``(before, after)`` per case. Discordant pairs (b, c) drive the test;
    the p-value is the two-sided binomial tail with p=0.5.
    """
    b = sum(1 for before, after in pairs if before and not after)
    c = sum(1 for before, after in pairs if not before and after)
    discordant = b + c
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(0, min(b, c) + 1)) / (
            2**discordant
        )
        p_value = min(1.0, 2 * tail)
    return {
        "test": "mcnemar_exact",
        "discordant_before_only": b,
        "discordant_after_only": c,
        "pairs": len(pairs),
        "p_value": round(p_value, 6),
    }


def holm_bonferroni(p_values: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values, preserving input keys."""
    if not p_values:
        return {}
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (key, p_value) in enumerate(ordered):
        candidate = min(1.0, (count - index) * p_value)
        running = max(running, candidate)
        adjusted[key] = round(running, 6)
    return {key: adjusted[key] for key in p_values}


def minimum_detectable_effect(
    n: int,
    baseline_rate: float,
    confidence_level: float = 0.95,
    power: float = 0.8,
) -> float | None:
    """Approximate absolute effect detectable with ``n`` cases per arm.

    This is an unpaired two-proportion approximation, so it is an upper bound for a paired
    design such as McNemar. It is reported to prevent over-reading a small sample, not as
    a guarantee.
    """
    if n <= 0 or not 0 < baseline_rate < 1 or not 0 < power < 1:
        return None
    z_alpha = _z_value(confidence_level)
    z_beta = NormalDist().inv_cdf(power)
    return round(
        min(1.0, (z_alpha + z_beta) * math.sqrt(2 * baseline_rate * (1 - baseline_rate) / n)),
        4,
    )


def summarize_replicates(
    results: list[dict[str, Any]],
    arm: str,
    metric: str,
) -> dict[str, Any]:
    """Aggregate a per-arm metric across replicates.

    Rows without the metric (``None``) or from other arms are skipped. Returns ``None``
    values when nothing is available so callers can mark the metric as missing.
    """
    values = [
        float(row[metric])
        for row in results
        if row.get("arm") == arm and row.get(metric) is not None
    ]
    if not values:
        return {
            "replicates": 0,
            "mean": None,
            "sd": None,
            "min": None,
            "max": None,
        }
    avg = mean(values)
    # Sample SD (n-1) is the conventional replicate-to-replicate spread; a single
    # replicate has no spread, so report zero rather than an undefined value.
    denominator = len(values) - 1 if len(values) > 1 else 1
    variance = sum((value - avg) ** 2 for value in values) / denominator
    return {
        "replicates": len(values),
        "mean": round(avg, 4),
        "sd": round(math.sqrt(variance), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def replicate_rate_sd(
    results: list[dict[str, Any]],
    arm: str,
    metric: str,
    kind: str,
) -> float | None:
    """Sample SD of a per-replicate rate for one arm.

    Returns ``None`` when no replicates have data and ``0.0`` when only one
    replicate is available (no observed spread).
    """
    by_replicate: dict[Any, list[bool]] = {}
    for row in results:
        if row.get("arm") != arm or row.get("kind") != kind or row.get("error"):
            continue
        value = row.get(metric)
        if value is None:
            continue
        replicate = row.get("replicate", 0)
        by_replicate.setdefault(replicate, []).append(bool(value))
    rates = [mean(values) for values in by_replicate.values() if values]
    if len(rates) < 2:
        return 0.0 if rates else None
    avg = mean(rates)
    variance = sum((rate - avg) ** 2 for rate in rates) / (len(rates) - 1)
    return round(math.sqrt(variance), 4)


def _cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [
        row["cost"]
        for row in rows
        if isinstance(row.get("cost"), dict) and row["cost"].get("priced")
    ]
    notional = sum(float(row["cost"].get("notional_usd") or 0.0) for row in priced)
    marginal = sum(float(row["cost"].get("marginal_usd") or 0.0) for row in priced)
    attacks = [
        row
        for row in rows
        if row.get("kind") == "attack"
        and isinstance(row.get("cost"), dict)
        and row["cost"].get("priced")
    ]
    attack_notional = sum(float(row["cost"].get("notional_usd") or 0.0) for row in attacks)
    blocked = sum(
        1
        for row in rows
        if row.get("kind") == "attack"
        and row.get("attack_success") is False
        and not row.get("error")
    )
    return {
        "priced_requests": len(priced),
        "unpriced_requests": len(rows) - len(priced),
        "notional_usd": round(notional, 8) if priced else None,
        "marginal_usd": round(marginal, 8) if priced else None,
        "attack_notional_usd": round(attack_notional, 8) if attacks else None,
        "blocked_attacks": blocked,
        "cost_per_blocked_attack_usd": (
            round(attack_notional / blocked, 8) if attacks and blocked else None
        ),
    }


def summarize_results(
    results: list[dict[str, Any]],
    arms: list[str],
    *,
    seed: int,
    iterations: int = 1000,
    confidence_level: float = 0.95,
    exploratory_min_n: int = 5,
    baseline_arm: str = "off",
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

        attack_successes = sum(attack_outcomes)
        false_refusal_count = sum(false_refusals)
        summaries[arm] = {
            "total": len(arm_results),
            "completed": len(successful),
            "errors": len(arm_results) - len(successful),
            "attack_count": len(attack_results),
            "attack_successes": attack_successes,
            "attack_success_rate": (
                round(mean(attack_outcomes), 4) if attack_outcomes else None
            ),
            "attack_success_ci95": bootstrap_mean_ci(
                attack_outcomes, seed + arm_index, iterations, confidence_level
            ),
            "attack_success_wilson95": wilson_ci(
                attack_successes, len(attack_outcomes), confidence_level
            ),
            "attack_mde_pp": minimum_detectable_effect(
                len(attack_outcomes),
                0.5,
                confidence_level,
            ),
            "replicate_sd": replicate_rate_sd(results, arm, "attack_success", "attack"),
            "false_refusal_replicate_sd": replicate_rate_sd(
                results, arm, "false_refusal", "benign"
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
                    "wilson95": wilson_ci(sum(outcomes), len(outcomes), confidence_level),
                    "exploratory": len(outcomes) < exploratory_min_n,
                }
                for category_index, (category, outcomes) in enumerate(sorted(by_category.items()))
            },
            "benign_count": len(benign_results),
            "false_refusals": false_refusal_count,
            "false_refusal_rate": (
                round(mean(false_refusals), 4) if false_refusals else None
            ),
            "false_refusal_ci95": bootstrap_mean_ci(
                false_refusals, seed + 100 + arm_index, iterations, confidence_level
            ),
            "false_refusal_wilson95": wilson_ci(
                false_refusal_count, len(false_refusals), confidence_level
            ),
            "benign_task_success_rate": (
                round(mean(benign_successes), 4) if benign_successes else None
            ),
            "guardrail_blocked": sum(bool(row.get("guardrail_blocked")) for row in arm_results),
            "p50_latency_ms": percentile(latencies, 0.50),
            "p95_latency_ms": percentile(latencies, 0.95),
            "p50_guardrail_latency_ms": percentile(guardrail_latencies, 0.50),
            "cost": _cost_summary(arm_results),
        }

    baseline = [
        row for row in results if row["arm"] == baseline_arm and not row.get("error")
    ]
    baseline_by_kind: dict[str, dict[str, dict[str, bool]]] = {"attack": {}, "benign": {}}
    for row in baseline:
        kind = row["kind"]
        metric = "attack_success" if kind == "attack" else "false_refusal"
        value = row.get(metric)
        if value is not None:
            baseline_by_kind[kind][row["case_id"]] = {metric: bool(value)}

    attack_p_values: dict[str, float] = {}
    false_refusal_p_values: dict[str, float] = {}
    for arm_index, arm in enumerate(arms):
        if arm == baseline_arm or arm not in summaries:
            continue
        treatment = [row for row in results if row["arm"] == arm and not row.get("error")]
        paired: dict[str, list[tuple[bool, bool]]] = {"attack": [], "benign": []}
        deltas: dict[str, list[float]] = {"attack": [], "benign": []}
        for row in treatment:
            kind = row["kind"]
            metric = "attack_success" if kind == "attack" else "false_refusal"
            before = baseline_by_kind[kind].get(row["case_id"])
            after = row.get(metric)
            if before is not None and after is not None:
                paired[kind].append((bool(before[metric]), bool(after)))
                deltas[kind].append(float(bool(after)) - float(before[metric]))

        attack_test = mcnemar_exact(paired["attack"])
        benign_test = mcnemar_exact(paired["benign"])
        if paired["attack"]:
            attack_p_values[arm] = attack_test["p_value"]
        if paired["benign"]:
            false_refusal_p_values[arm] = benign_test["p_value"]

        summaries[arm]["paired_vs_off"] = {
            "attack_cases": len(paired["attack"]),
            "attack_success_delta": (
                round(mean(deltas["attack"]), 4) if deltas["attack"] else None
            ),
            "attack_success_delta_ci95": bootstrap_mean_ci(
                deltas["attack"], seed + 200 + arm_index, iterations, confidence_level
            ),
            "attack_success_mcnemar": attack_test,
            "benign_cases": len(paired["benign"]),
            "false_refusal_delta": (
                round(mean(deltas["benign"]), 4) if deltas["benign"] else None
            ),
            "false_refusal_delta_ci95": bootstrap_mean_ci(
                deltas["benign"], seed + 300 + arm_index, iterations, confidence_level
            ),
            "false_refusal_mcnemar": benign_test,
        }

    attack_adjusted = holm_bonferroni(attack_p_values)
    benign_adjusted = holm_bonferroni(false_refusal_p_values)
    for arm, adjusted in attack_adjusted.items():
        paired_summary = summaries[arm]["paired_vs_off"]
        paired_summary["attack_success_p_holm"] = adjusted
        paired_summary["attack_success_significant_holm"] = adjusted < (1 - confidence_level)
    for arm, adjusted in benign_adjusted.items():
        paired_summary = summaries[arm]["paired_vs_off"]
        paired_summary["false_refusal_p_holm"] = adjusted
        paired_summary["false_refusal_significant_holm"] = adjusted < (1 - confidence_level)

    return summaries
