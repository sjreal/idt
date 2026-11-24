"""Statistical primitive tests: intervals, paired tests, multiplicity, MDE."""


import pytest

from security_eval.analysis import (
    holm_bonferroni,
    mcnemar_exact,
    minimum_detectable_effect,
    replicate_rate_sd,
    summarize_replicates,
    wilson_ci,
)


def test_wilson_ci_matches_known_example() -> None:
    # Agresti-Coull style reference values for x=5, n=10 at 95%.
    interval = wilson_ci(5, 10)
    assert interval is not None
    assert interval["low"] == pytest.approx(0.2366, abs=1e-3)
    assert interval["high"] == pytest.approx(0.7634, abs=1e-3)


def test_wilson_ci_extremes_stay_in_unit_interval() -> None:
    zero = wilson_ci(0, 20)
    full = wilson_ci(20, 20)
    assert zero is not None and full is not None
    assert zero["low"] == 0.0
    assert 0 < zero["high"] < 1
    assert full["high"] == 1.0
    assert 0 < full["low"] < 1


def test_wilson_ci_invalid_inputs() -> None:
    assert wilson_ci(0, 0) is None
    with pytest.raises(ValueError):
        wilson_ci(11, 10)
    with pytest.raises(ValueError):
        wilson_ci(1, 10, confidence_level=1.0)


def test_mcnemar_exact_known_small_table() -> None:
    # b=3, c=0, n=3 discordant -> two-sided p = 2 * (1/2)^3 = 0.25
    pairs = [(True, False)] * 3 + [(True, True)] * 5
    result = mcnemar_exact(pairs)

    assert result["test"] == "mcnemar_exact"
    assert result["discordant_before_only"] == 3
    assert result["discordant_after_only"] == 0
    assert result["pairs"] == 8
    assert result["p_value"] == pytest.approx(0.25)


def test_mcnemar_exact_no_discordant_pairs() -> None:
    result = mcnemar_exact([(True, True), (False, False)])
    assert result["p_value"] == 1.0
    assert result["discordant_before_only"] == 0
    assert result["discordant_after_only"] == 0


def test_mcnemar_exact_symmetric_discordance_is_not_significant() -> None:
    pairs = [(True, False)] * 4 + [(False, True)] * 4
    result = mcnemar_exact(pairs)
    assert result["p_value"] == pytest.approx(1.0)


def test_mcnemar_exact_empty_input() -> None:
    result = mcnemar_exact([])
    assert result["pairs"] == 0
    assert result["p_value"] == 1.0


def test_holm_bonferroni_is_monotonic_and_preserves_keys() -> None:
    adjusted = holm_bonferroni({"a": 0.01, "b": 0.04, "c": 0.03})

    assert list(adjusted) == ["a", "b", "c"]
    # Sorted raw p-values: a=0.01, c=0.03, b=0.04 (n=3).
    # Holm multipliers: a x3=0.03, c x2=0.06, b x1=0.04 -> running max caps b at 0.06.
    assert adjusted["a"] == pytest.approx(0.03)
    assert adjusted["c"] == pytest.approx(0.06)
    assert adjusted["b"] == pytest.approx(0.06)
    # Adjusted p-values never decrease with raw p-value rank.
    assert adjusted["a"] <= adjusted["c"] <= adjusted["b"]


def test_holm_bonferroni_caps_at_one() -> None:
    assert holm_bonferroni({"a": 0.9, "b": 0.8})["a"] == 1.0
    assert holm_bonferroni({}) == {}


def test_minimum_detectable_effect_shrinks_with_n() -> None:
    small = minimum_detectable_effect(10, 0.5)
    large = minimum_detectable_effect(100, 0.5)
    assert small is not None and large is not None
    assert small > large
    assert 0 < large < 1


def test_minimum_detectable_effect_invalid_returns_none() -> None:
    assert minimum_detectable_effect(0, 0.5) is None
    assert minimum_detectable_effect(10, 0.0) is None
    assert minimum_detectable_effect(10, 1.0) is None


def test_summarize_replicates_reports_mean_sd_and_spread() -> None:
    values = [
        {"arm": "off", "replicate": 1, "attack_success": 0.2},
        {"arm": "off", "replicate": 2, "attack_success": 0.4},
        {"arm": "off", "replicate": 3, "attack_success": 0.6},
    ]

    summary = summarize_replicates(values, "off", "attack_success")

    assert summary["replicates"] == 3
    assert summary["mean"] == pytest.approx(0.4)
    assert summary["sd"] == pytest.approx(0.2)
    assert summary["min"] == pytest.approx(0.2)
    assert summary["max"] == pytest.approx(0.6)


def test_summarize_replicates_single_value_has_zero_sd() -> None:
    values = [{"arm": "full", "replicate": 1, "attack_success": 0.1}]
    summary = summarize_replicates(values, "full", "attack_success")
    assert summary["replicates"] == 1
    assert summary["sd"] == 0.0


def test_summarize_replicates_missing_values_are_skipped() -> None:
    values = [
        {"arm": "off", "replicate": 1, "attack_success": None},
        {"arm": "off", "replicate": 2, "attack_success": 0.5},
    ]
    summary = summarize_replicates(values, "off", "attack_success")
    assert summary["replicates"] == 1
    assert summary["mean"] == pytest.approx(0.5)


def test_replicate_rate_sd_groups_by_replicate_before_spread() -> None:
    rows = [
        {"arm": "off", "kind": "attack", "replicate": 0, "attack_success": True},
        {"arm": "off", "kind": "attack", "replicate": 0, "attack_success": False},
        {"arm": "off", "kind": "attack", "replicate": 1, "attack_success": True},
        {"arm": "off", "kind": "attack", "replicate": 1, "attack_success": True},
        {"arm": "off", "kind": "attack", "replicate": 2, "attack_success": False},
        {"arm": "off", "kind": "attack", "replicate": 2, "attack_success": True},
    ]
    # Per-replicate ASRs are 0.5, 1.0, 0.5, so the spread is non-zero even
    # though the pooled ASR across all six rows is exactly 0.5.
    assert replicate_rate_sd(rows, "off", "attack_success", "attack") == pytest.approx(
        0.2887, abs=0.0001
    )

    rows[3]["attack_success"] = False
    assert replicate_rate_sd(rows, "off", "attack_success", "attack") == pytest.approx(0.0)


def test_replicate_rate_sd_single_replicate_has_zero_spread() -> None:
    rows = [{"arm": "full", "kind": "attack", "replicate": 0, "attack_success": True}]
    assert replicate_rate_sd(rows, "full", "attack_success", "attack") == 0.0
