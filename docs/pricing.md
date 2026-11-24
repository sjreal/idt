# Pricing and cost accounting

All cost numbers come from one committed snapshot: [`data/pricing/opencode-go.json`](../data/pricing/opencode-go.json). Request handling never fetches live prices, so runs remain reproducible and a broken price site cannot break chat.

## Snapshot provenance

| Field | Value |
| --- | --- |
| Provider | `opencode-go` |
| Schema version | 1 |
| Retrieved at | 2026-11-23 |
| Source | <https://models.dev/api.json> |
| Provider docs | <https://opencode.ai/v2/docs/console/go/> |
| Billing mode | `subscription` |
| Currency | USD |
| Integrity | `integrity.sha256 = 00ff119cf3c9b7eb50d4de6f8e6abaf96c735365cdba137fa7d2eaa4a9b57ef2` |

`src/llm_app/pricing.py` computes the checksum over the canonical JSON with the `integrity` block removed and refuses to load a snapshot whose checksum does not match. The snapshot records **19** Chat Completions-compatible models, matching the backend allowlist. The default model is `deepseek-v4.1-flash`; `beam-free` is included with zero rates.

## Cost semantics

- **Notional USD** is the list-price equivalent: input, cached-input, and output tokens multiplied by the pinned per-million-token rates. It is the primary number for cross-model comparison.
- **Marginal USD** is the incremental charge attributed to a request. OpenCode Go is subscription-backed, so marginal USD is `0` inside the plan allowance. Quota consumption is the scarce resource and is tracked separately.
- **`priced=false` is never imputed.** Models absent from the snapshot (`model_not_in_catalog`) or with unpublished rates (`price_unpublished`, e.g. `kimi-k2.6` at the recorded date) return `null` costs and are excluded from sums. Reports list them in the unpriced appendix.
- `beam-free` has explicit zero rates; it is priced at `0.0`, not unknown.
- Report **both** notional and marginal values. A zero marginal figure must not be read as "free" without the subscription/quota context.

Guardrail costs in the snapshot:

| Component | Billing | Treatment |
| --- | --- | --- |
| LLM Guard scanners | local CPU | `usd_per_request = 0`; zero marginal USD |
| Ollama Llama Guard | local | `usd_per_request = 0`; zero marginal USD |
| Cloudflare Workers AI | neurons, 10,000 free/day, $11 per 1M neurons | Per-token neuron rate for `@cf/meta/llama-guard-3-8b` is unpublished, so guardrail cost remains unpriced; token counts are recorded |

## Where costs appear

- `GET /api/pricing` returns the catalog document, provenance, `pricing_sha256`, per-model entries, and guardrail entries.
- Chat and OpenAI-compatible responses include a `cost` object with `priced`, `notional_usd`, `marginal_usd`, `billing_mode`, `pricing_sha256`, and `unpriced_reason`.
- A pricing-load error is logged and returns `cost: null`; it does not fail generation.
- Evaluation result rows store the same `cost` object. Analysis totals count only `priced=true` rows and expose `priced_requests`, `unpriced_requests`, `notional_usd`, `marginal_usd`, `attack_notional_usd`, and `cost_per_blocked_attack_usd`.
- Prometheus records notional/marginal generation cost and allowance gauges. See [Observability](observability.md).

## Refreshing

Refresh is manual and explicit:

```sh
uv run python scripts/refresh_pricing.py            # preview the diff
uv run python scripts/refresh_pricing.py --write    # update the snapshot
```

Review provider documentation for tiers, regions, and promotional rates before committing. Update `retrieved_at`/`source_url` through the script and keep the README and checksum consistent. The planned `make pricing-refresh` alias is **not** present in the current Makefile; use the script directly.

See [`data/pricing/README.md`](../data/pricing/README.md) for the snapshot's short reference and [Comparison and statistics](comparison.md) for how cost enters reports.
