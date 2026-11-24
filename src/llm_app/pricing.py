"""Versioned pricing catalog and per-request cost accounting.

The catalog is a committed snapshot so cost numbers are reproducible. Refresh it with
``scripts/refresh_pricing.py`` (manual, network) and commit the result, never fetch prices
inside a request path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "pricing" / "opencode-go.json"
SUPPORTED_SCHEMA_VERSIONS = {1}
TOKENS_PER_MILLION = 1_000_000


class PricingError(Exception):
    """Raised when the pricing snapshot is missing or internally inconsistent."""


@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    input_per_mtok: float | None
    output_per_mtok: float | None
    cached_input_per_mtok: float | None
    free: bool
    context_limit: int | None
    output_limit: int | None
    notes: str | None


@dataclass(frozen=True)
class PricingCatalog:
    provider: str
    schema_version: int
    retrieved_at: str
    source_url: str
    billing_mode: str
    currency: str
    models: Mapping[str, ModelPrice]
    guardrails: Mapping[str, Any]
    sha256: str
    raw: Mapping[str, Any]

    def model(self, model_id: str) -> ModelPrice | None:
        return self.models.get(model_id)


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    """Canonical bytes for integrity checks, excluding the integrity block itself."""
    body = {key: value for key, value in payload.items() if key != "integrity"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_integrity(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def _optional_rate(value: Any, field: str, model_id: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"Invalid {field} for {model_id!r}")
    if value < 0:
        raise PricingError(f"Negative {field} for {model_id!r}")
    return float(value)


def _optional_limit(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingError("Invalid model token limit")
    return value


def load_catalog(path: Path | str = DEFAULT_CATALOG_PATH) -> PricingCatalog:
    catalog_path = Path(path)
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PricingError(f"Pricing catalog not found: {catalog_path}") from exc
    except json.JSONDecodeError as exc:
        raise PricingError(f"Pricing catalog is not valid JSON: {catalog_path}") from exc
    if not isinstance(payload, dict):
        raise PricingError("Pricing catalog must be a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PricingError(f"Unsupported pricing schema version: {schema_version!r}")

    integrity = payload.get("integrity")
    expected = integrity.get("sha256") if isinstance(integrity, dict) else None
    if not expected:
        raise PricingError("Pricing catalog is missing integrity.sha256")
    actual = compute_integrity(payload)
    if actual != expected:
        raise PricingError(
            f"Pricing catalog checksum mismatch: expected {expected}, got {actual}"
        )

    raw_models = payload.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise PricingError("Pricing catalog has no models")

    models: dict[str, ModelPrice] = {}
    for model_id, entry in raw_models.items():
        if not isinstance(entry, dict):
            raise PricingError(f"Invalid pricing entry for {model_id!r}")
        limits = entry.get("limits") or {}
        if not isinstance(limits, dict):
            limits = {}
        models[str(model_id)] = ModelPrice(
            model_id=str(model_id),
            input_per_mtok=_optional_rate(entry.get("input_per_mtok"), "input", model_id),
            output_per_mtok=_optional_rate(entry.get("output_per_mtok"), "output", model_id),
            cached_input_per_mtok=_optional_rate(
                entry.get("cached_input_per_mtok"), "cached_input", model_id
            ),
            free=bool(entry.get("free", False)),
            context_limit=_optional_limit(limits.get("context")),
            output_limit=_optional_limit(limits.get("output")),
            notes=entry.get("notes"),
        )

    guardrails = payload.get("guardrails") or {}
    if not isinstance(guardrails, dict):
        raise PricingError("Pricing catalog guardrails must be an object")

    return PricingCatalog(
        provider=str(payload.get("provider", "")),
        schema_version=int(schema_version),
        retrieved_at=str(payload.get("retrieved_at", "")),
        source_url=str(payload.get("source_url", "")),
        billing_mode=str(payload.get("billing_mode", "unknown")),
        currency=str(payload.get("currency", "USD")),
        models=models,
        guardrails=guardrails,
        sha256=actual,
        raw=payload,
    )


@lru_cache
def get_catalog() -> PricingCatalog:
    return load_catalog()


def _round_usd(value: float) -> float:
    return round(value, 12)


def generation_cost(
    catalog: PricingCatalog,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> dict[str, Any]:
    """Return a structured cost breakdown for one generation request.

    ``notional_usd`` is the list-price equivalent; ``marginal_usd`` is what an incremental
    request would bill. A subscription-backed provider has zero marginal USD inside its
    allowance, so quota is tracked separately.
    """
    prompt_tokens = max(0, int(prompt_tokens))
    completion_tokens = max(0, int(completion_tokens))
    cached_tokens = max(0, min(int(cached_input_tokens), prompt_tokens))

    base: dict[str, Any] = {
        "model": model_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_input_tokens": cached_tokens,
        "currency": catalog.currency,
        "billing_mode": catalog.billing_mode,
        "pricing_sha256": catalog.sha256,
        "priced": False,
        "notional_usd": None,
        "marginal_usd": None,
        "components": {},
        "unpriced_reason": None,
    }

    price = catalog.model(model_id)
    if price is None:
        base["unpriced_reason"] = "model_not_in_catalog"
        return base
    if price.free:
        base.update(
            priced=True,
            notional_usd=0.0,
            marginal_usd=0.0,
            components={"input_usd": 0.0, "cached_input_usd": 0.0, "output_usd": 0.0},
        )
        return base
    if price.input_per_mtok is None or price.output_per_mtok is None:
        base["unpriced_reason"] = "price_unpublished"
        return base

    cached_rate = price.cached_input_per_mtok
    if cached_rate is None:
        cached_tokens = 0
        cached_rate = 0.0
    uncached_tokens = prompt_tokens - cached_tokens

    input_usd = uncached_tokens / TOKENS_PER_MILLION * price.input_per_mtok
    cached_usd = cached_tokens / TOKENS_PER_MILLION * cached_rate
    output_usd = completion_tokens / TOKENS_PER_MILLION * price.output_per_mtok
    notional = input_usd + cached_usd + output_usd
    marginal = 0.0 if catalog.billing_mode in {"subscription", "local"} else notional

    base.update(
        priced=True,
        notional_usd=_round_usd(notional),
        marginal_usd=_round_usd(marginal),
        components={
            "input_usd": _round_usd(input_usd),
            "cached_input_usd": _round_usd(cached_usd),
            "output_usd": _round_usd(output_usd),
        },
    )
    return base


def catalog_document(catalog: PricingCatalog) -> dict[str, Any]:
    """Public, JSON-safe view for the pricing endpoint."""
    return {
        "provider": catalog.provider,
        "schema_version": catalog.schema_version,
        "retrieved_at": catalog.retrieved_at,
        "source_url": catalog.source_url,
        "billing_mode": catalog.billing_mode,
        "currency": catalog.currency,
        "pricing_sha256": catalog.sha256,
        "models": [
            {
                "id": price.model_id,
                "input_per_mtok": price.input_per_mtok,
                "output_per_mtok": price.output_per_mtok,
                "cached_input_per_mtok": price.cached_input_per_mtok,
                "free": price.free,
                "context_limit": price.context_limit,
                "output_limit": price.output_limit,
                "priced": price.free
                or (price.input_per_mtok is not None and price.output_per_mtok is not None),
                "notes": price.notes,
            }
            for price in sorted(catalog.models.values(), key=lambda item: item.model_id)
        ],
        "guardrails": catalog.guardrails,
    }
