# Pricing snapshot

`opencode-go.json` is a committed, versioned snapshot used for cost accounting. Request
handling never fetches prices.

## What the numbers mean

- **Units:** USD per million tokens (input, cached input, output).
- **Notional cost:** list-price equivalent, used to compare models.
- **Marginal cost:** what an additional request bills. OpenCode Go is a subscription, so
  marginal USD is `0` inside the plan allowance. Quota consumption is the real scarce
  resource and is tracked separately.
- **Free models:** `beam-free` has zero rates; it is priced, not unknown.
- **Unpriced models:** `null` rates mean the source did not publish them (`kimi-k2.6` at
  the recorded date). These requests are counted as `unpriced` and excluded from sums.
  Nothing is imputed.

Guardrail costs:

- LLM Guard and Ollama run locally: marginal USD `0`.
- Cloudflare Workers AI bills in neurons at **$0.011 per 1,000 neurons** with a free
  allocation of **10,000 neurons/day**. A per-token neuron rate for
  `@cf/meta/llama-guard-3-8b` is not published on its model page, so guardrail cost is
  currently **unpriced** while token counts are recorded.

## Provenance

The file records `retrieved_at`, `source_url` (models.dev, provider `opencode-go`), the
provider documentation URL, and an `integrity.sha256` computed over the canonical JSON
with the integrity block removed. `src/llm_app/pricing.py` refuses to load a snapshot whose
checksum does not match.

## Refreshing

```sh
uv run python scripts/refresh_pricing.py            # preview the diff
uv run python scripts/refresh_pricing.py --write    # update the snapshot
```

Review the diff, check the provider documentation for anything unusual (tiers, region
pricing, promotional rates), then commit the snapshot. Do not edit prices by hand without
recording why in the commit message.

`tests/test_pricing.py` verifies the committed snapshot loads, its checksum matches, and
every supported Chat Completions model has an entry.
