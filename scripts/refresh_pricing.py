"""Refresh the committed model pricing snapshot from models.dev.

Network access is manual and explicit; request handling never fetches prices. Run:

    uv run python scripts/refresh_pricing.py            # preview
    uv run python scripts/refresh_pricing.py --write    # update the catalog

The catalog is regenerated for the Chat Completions models this app supports. Review the
diff, then commit it with the stated source and retrieval date.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_app.backends.opencode_go import OpenCodeGoBackend  # noqa: E402
from llm_app.pricing import (  # noqa: E402
    DEFAULT_CATALOG_PATH,
    compute_integrity,
)

MODELS_DEV_URL = "https://models.dev/api.json"
PROVIDER_ID = "opencode-go"
PROVIDER_DOC_URL = "https://opencode.ai/v2/docs/console/go/"
USER_AGENT = "guardrail-lab/0.1 pricing-refresh"


def fetch_models_dev(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("models.dev returned an unexpected payload")
    return payload


def build_catalog(payload: dict[str, Any], retrieved_at: str) -> dict[str, Any]:
    provider = payload.get(PROVIDER_ID)
    if not isinstance(provider, dict):
        raise ValueError(f"Provider {PROVIDER_ID!r} is missing from models.dev")
    upstream_models = provider.get("models") or {}
    if not isinstance(upstream_models, dict):
        raise ValueError("Provider models payload is invalid")

    catalog_models: dict[str, Any] = {}
    for model_id in sorted(OpenCodeGoBackend.chat_completion_models):
        entry = upstream_models.get(model_id)
        if not isinstance(entry, dict):
            catalog_models[model_id] = {
                "input_per_mtok": None,
                "output_per_mtok": None,
                "cached_input_per_mtok": None,
                "free": False,
                "limits": {},
                "notes": "Not present in the upstream snapshot; pricing unknown.",
            }
            continue
        cost = entry.get("cost") or {}
        limits = entry.get("limit") or {}
        rates = {
            "input_per_mtok": cost.get("input"),
            "output_per_mtok": cost.get("output"),
            "cached_input_per_mtok": cost.get("cache_read"),
        }
        known = [value for value in rates.values() if isinstance(value, (int, float))]
        free = bool(known) and all(float(value) == 0 for value in known)
        catalog_models[model_id] = {
            **rates,
            "free": free,
            "limits": {
                "context": limits.get("context"),
                "output": limits.get("output"),
            },
            "notes": entry.get("name"),
        }

    return {
        "schema_version": 1,
        "provider": PROVIDER_ID,
        "billing_mode": "subscription",
        "currency": "USD",
        "retrieved_at": retrieved_at,
        "source_url": MODELS_DEV_URL,
        "provider_doc_url": PROVIDER_DOC_URL,
        "notes": (
            "Notional list-price equivalent for OpenCode Go (subscription plan). "
            "marginal_usd is zero inside the plan allowance; quota is tracked separately. "
            "Prices are USD per million tokens."
        ),
        "models": catalog_models,
        "guardrails": {
            "cloudflare-workers-ai": {
                "billing": "neurons",
                "usd_per_million_neurons": 11.0,
                "free_neurons_per_day": 10000,
                "source_url": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
                "retrieved_date": "2026-11-23",
                "models": {
                    "@cf/meta/llama-guard-3-8b": {
                        "neurons_per_million_input_tokens": None,
                        "notes": (
                            "Cloudflare does not publish a per-token neuron rate on the "
                            "model page. Token counts are recorded; guardrail cost stays "
                            "unpriced until a rate is available."
                        ),
                    }
                },
            },
            "llm-guard": {
                "billing": "local",
                "usd_per_request": 0.0,
                "notes": "Runs on local CPU; marginal USD is zero.",
            },
            "ollama": {
                "billing": "local",
                "usd_per_request": 0.0,
                "notes": "Runs locally; marginal USD is zero.",
            },
        },
        "integrity": {"algorithm": "sha256", "sha256": ""},
    }


def diff_models(current: dict[str, Any], updated: dict[str, Any]) -> list[str]:
    current_models = current.get("models") or {}
    updated_models = updated.get("models") or {}
    lines: list[str] = []
    for model_id in sorted(set(current_models) | set(updated_models)):
        before = current_models.get(model_id)
        after = updated_models.get(model_id)
        if before == after:
            continue
        if before is None:
            lines.append(f"+ {model_id}")
        elif after is None:
            lines.append(f"- {model_id}")
        else:
            lines.append(f"~ {model_id}: {before} -> {after}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the catalog file")
    parser.add_argument("--path", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--source", default=MODELS_DEV_URL)
    parser.add_argument(
        "--retrieved-at",
        default=datetime.now(UTC).date().isoformat(),
        help="ISO date recorded in the snapshot",
    )
    args = parser.parse_args()

    try:
        upstream = fetch_models_dev(args.source)
        updated = build_catalog(upstream, args.retrieved_at)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        print(f"Could not refresh pricing: {exc}", file=sys.stderr)
        return 1

    updated["integrity"]["sha256"] = compute_integrity(updated)

    current: dict[str, Any] = {}
    if args.path.exists():
        try:
            current = json.loads(args.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}

    changes = diff_models(current, updated)
    print(f"Provider {PROVIDER_ID}: {len(updated['models'])} supported models")
    print(f"Retrieved at {args.retrieved_at}; pricing sha256 {updated['integrity']['sha256']}")
    if changes:
        print("\n".join(changes))
    else:
        print("No model pricing changes.")

    if not args.write:
        print("Preview only. Re-run with --write to update the catalog.")
        return 0

    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(
        json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
