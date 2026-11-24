"""Pricing snapshot integrity and cost-accounting tests (no network, no providers)."""

import json
from pathlib import Path

import pytest

from llm_app.backends.opencode_go import OpenCodeGoBackend
from llm_app.pricing import (
    DEFAULT_CATALOG_PATH,
    PricingError,
    canonical_payload,
    compute_integrity,
    generation_cost,
    get_catalog,
    load_catalog,
)


def test_committed_catalog_loads_and_checksum_matches() -> None:
    catalog = load_catalog()

    assert catalog.provider == "opencode-go"
    assert catalog.billing_mode == "subscription"
    assert catalog.currency == "USD"
    assert catalog.source_url.startswith("https://")
    assert catalog.retrieved_at
    assert len(catalog.sha256) == 64
    assert compute_integrity(catalog.raw) == catalog.sha256


def test_every_supported_chat_model_has_a_catalog_entry() -> None:
    catalog = load_catalog()

    for model_id in OpenCodeGoBackend.chat_completion_models:
        assert catalog.model(model_id) is not None, model_id


def test_canonical_payload_ignores_integrity_block() -> None:
    payload = {"schema_version": 1, "models": {}, "integrity": {"sha256": "x"}}
    assert canonical_payload(payload) == canonical_payload(
        {"schema_version": 1, "models": {}}
    )


def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    payload["integrity"]["sha256"] = "0" * 64
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PricingError, match="checksum mismatch"):
        load_catalog(tampered)


def test_missing_catalog_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PricingError, match="not found"):
        load_catalog(tmp_path / "missing.json")


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PricingError, match="Unsupported pricing schema"):
        load_catalog(bad)


def test_known_rates_produce_expected_notional_cost() -> None:
    catalog = load_catalog()
    price = catalog.model("deepseek-v4.1-flash")
    assert price is not None
    assert price.input_per_mtok == 0.15
    assert price.output_per_mtok == 0.6

    breakdown = generation_cost(catalog, "deepseek-v4.1-flash", 1_000_000, 1_000_000)

    assert breakdown["priced"] is True
    assert breakdown["notional_usd"] == pytest.approx(0.75)
    # Subscription-backed Go: incremental USD is zero inside the plan allowance.
    assert breakdown["marginal_usd"] == 0.0
    assert breakdown["components"]["input_usd"] == pytest.approx(0.15)
    assert breakdown["components"]["output_usd"] == pytest.approx(0.6)


def test_cached_input_uses_cache_rate() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(
        catalog, "deepseek-v4.1-flash", 1_000_000, 0, cached_input_tokens=1_000_000
    )

    assert breakdown["cached_input_tokens"] == 1_000_000
    assert breakdown["components"]["cached_input_usd"] == pytest.approx(0.003)
    assert breakdown["components"]["input_usd"] == 0.0


def test_free_model_is_priced_at_zero() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(catalog, "beam-free", 1_000_000, 1_000_000)

    assert breakdown["priced"] is True
    assert breakdown["notional_usd"] == 0.0
    assert breakdown["marginal_usd"] == 0.0


def test_unpriced_model_is_explicit_and_not_imputed() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(catalog, "not-a-real-model", 100, 100)

    assert breakdown["priced"] is False
    assert breakdown["unpriced_reason"] == "model_not_in_catalog"
    assert breakdown["notional_usd"] is None
    assert breakdown["marginal_usd"] is None


def test_unpublished_price_is_explicit() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(catalog, "kimi-k2.6", 100, 100)

    assert breakdown["priced"] is False
    assert breakdown["unpriced_reason"] == "price_unpublished"


def test_zero_usage_is_priced_at_zero() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(catalog, "deepseek-v4.1-flash", 0, 0)

    assert breakdown["priced"] is True
    assert breakdown["notional_usd"] == 0.0
    assert breakdown["marginal_usd"] == 0.0


def test_cached_tokens_cannot_exceed_prompt_tokens() -> None:
    catalog = load_catalog()
    breakdown = generation_cost(
        catalog, "deepseek-v4.1-flash", 100, 50, cached_input_tokens=999
    )
    assert breakdown["cached_input_tokens"] == 100


def test_get_catalog_is_cached() -> None:
    assert get_catalog() is get_catalog()
