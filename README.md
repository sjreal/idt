# LLM Guardrail Lab

A **localhost-only** LLM playground and reproducible study of open-source guardrails. It compares an unguarded baseline with layered input/output protections and records attack success, false refusals, latency, cost, and operational evidence.

This is a research/demo lab. It is **not a production service**, has no customer SLA, and is not intended for public exposure. All Compose ports bind to `127.0.0.1`.

Authoritative plan: `/Users/siddharthjain/.opencode/plan/idt-guardrails-observability-pricing.md` ([open local file](file:///Users/siddharthjain/.opencode/plan/idt-guardrails-observability-pricing.md)).

## Research questions

| RQ | Question | Primary evidence | Where to read |
| --- | --- | --- | --- |
| RQ1 | Which guardrail layer reduces attack success, and by how much vs `off`? | ASR per arm, Wilson CI, paired exact McNemar, Holm adjustment, `blocked_by`/stage attribution | [Comparison and statistics](docs/comparison.md) |
| RQ2 | What do reductions cost in false refusals, task success, and latency? | False-refusal rate, benign task success, p50/p95 end-to-end and per-stage latency, bootstrap paired deltas | [Comparison and statistics](docs/comparison.md), [SLOs](docs/slo.md) |
| RQ3 | What does one blocked attack cost per model/layer? | Committed pricing snapshot, `notional_usd`, `marginal_usd`, classifier-call counts, unpriced appendix | [Pricing](docs/pricing.md) |
| RQ4 | Does the system degrade visibly under provider/quota/classifier failure? | Fallback and circuit-breaker metrics, eval abort status, guardrail fail-closed behavior, alerts | [Security and hardening](docs/security.md), [Observability](docs/observability.md), [SLOs](docs/slo.md) |
| RQ5 | Can telemetry detect and explain a guardrail regression? | Prometheus/Grafana aggregates, OTel spans, optional Phoenix traces, local JSONL trace buffer, redaction audit | [Observability](docs/observability.md), [SLOs](docs/slo.md) |

Unit of analysis is one `(case, arm, model, replicate)` request. Ground truth is synthetic: canary success markers for attacks and expected-term/refusal heuristics for benign prompts. Intervals describe this fixed sample, not a population of attacks.

## Three evidence planes

1. **Operations plane — Prometheus/Grafana.** Aggregate API, generation, guardrail, fallback, circuit, and evaluation metrics. Optional under the `observability` Compose profile. See [SLOs](docs/slo.md) and [Observability](docs/observability.md).
2. **Trace plane — OpenTelemetry + optional Phoenix.** Manual OTel GenAI semantic-convention spans exported over OTLP/HTTP when an endpoint is configured. Phoenix is optional; the app works when it is stopped. There is no Langfuse integration.
3. **Result/report plane — evaluation runs, JSONL, CSV, HTML/JSON reports.** The authoritative evidence for RQ1–RQ3. Reports exclude prompt/answer content by default. See [Comparison and statistics](docs/comparison.md).

## Quick start

```sh
cp .env.example .env
make dev
```

`make dev` runs `docker compose up --build -d`. With no Compose profiles it starts the API and frontend only.

| Surface | URL | Notes |
| --- | --- | --- |
| App UI | <http://localhost:3000> | Chat, evaluation, compare, trace, and model/cost workspaces |
| API | <http://localhost:8000> | `/health`, `/metrics`, `/v1/models`, `/api/*` |
| Swagger UI | <http://localhost:8000/docs> | Interactive OpenAPI docs |
| ReDoc | <http://localhost:8000/redoc> | Alternative API docs |
| OpenAPI JSON | <http://localhost:8000/openapi.json> | Machine-readable schema |
| Prometheus | <http://localhost:9090> | Only with `observability` profile |
| Grafana | <http://localhost:3001> | `admin` / `local-only-change-me` by default; only with `observability` profile |
| CI exporter | <http://localhost:9101/metrics> | Only with `observability` profile |
| Phoenix | <http://localhost:6006> | Only with `observability` profile **and** OTLP export configured |

### Choose a backend

**Ollama (local):**

```dotenv
LLM_BACKEND=ollama
COMPOSE_PROFILES=ollama
OLLAMA_MODEL=llama3.2:1b
```

```sh
make dev
make model-pull
```

For the optional local Llama Guard classifier, also set `LLAMA_GUARD_BACKEND=ollama`, run `make dev`, then `make guard-model-pull`.

**OpenCode Go (remote, allowlisted Chat Completions models):**

```dotenv
LLM_BACKEND=opencode-go
COMPOSE_PROFILES=
OPENCODE_GO_MODEL=deepseek-v4.1-flash
OPENCODE_GO_API_KEY=your-key-from-the-opencode-console
```

```sh
make dev
```

The allowlist contains **19** Chat Completions-compatible models. The default is `deepseek-v4.1-flash`; `beam-free` is selectable. Models served through Responses, Anthropic Messages, or Google endpoints are not exposed. The OpenCode Go key currently available for this work is expired, so live calls were not used during documentation verification; validation here is code-first.

Both chat and evaluation screens retain a generation-model selector. Evaluation runs pin the chosen model and disable fallback; a provider failure is surfaced as an error rather than silently swapping the model under test. Chat fallback is opt-in through `OPENCODE_GO_FALLBACK_MODELS` (empty by default).

### Optional observability

```sh
make observability-up     # Prometheus, Grafana, CI exporter, Phoenix
make observability-down   # stops Grafana, Prometheus, CI exporter, and Phoenix
```

`make observability-up` and `make observability-down` use `docker-compose.yml` plus `docker-compose.observability.yml`. The override points the app at the Phoenix OTLP/HTTP collector, so the profile wires the trace plane end to end; the base file alone leaves `OTEL_EXPORTER_OTLP_ENDPOINT` empty, so a plain `docker compose up` produces zero exporter traffic and does not require Phoenix. Content capture stays off unless explicitly enabled. See [Observability](docs/observability.md).

The normal app does **not** need Prometheus, Grafana, or Phoenix. `make check` is the intended provider-free validation path: Ruff, pytest, frontend build, and Compose config validation. It does not start evaluations, call model providers, or require Phoenix. GitHub Actions also uses a provider-free container smoke test.

## Guardrail arms

`full` keeps its original meaning. `adaptive` resolves per request after the prefilter.

| Arm | Prefilter | Local LLM Guard scanners | Classifier input | Classifier output | Behavior |
| --- | --- | --- | --- | --- | --- |
| `off` | – | – | – | – | Baseline; no checks |
| `prefilter` | ✅ | – | – | – | Cheap deterministic pattern score only |
| `scanners` | – | ✅ | – | – | PromptInjection blocks; Secrets redacts the latest user turn |
| `input` | – | ✅ | ✅ | – | Scan, redact/block, then classify input |
| `output` | – | – | – | ✅ | Classify generated answer only |
| `full` | – | ✅ | ✅ | ✅ | Legacy G2 meaning unchanged |
| `adaptive` | ✅ | on `review` | on `review` | on `review` | `block` or `pass` short-circuits; `review` runs the full plan |

Classifier = Cloudflare Workers AI Llama Guard 3 by default (`@cf/meta/llama-guard-3-8b`), or local Ollama Llama Guard when `LLAMA_GUARD_BACKEND=ollama`. Enabled checks fail closed: an unavailable scanner/classifier returns HTTP 503 instead of bypassing the check. If an enabled guardrail cannot run, the request is not sent to the model (input) or the answer is withheld (output).

The prefilter records the decision it reached — `blocked`, `review`, or `passed` — so a `review` score is never reported as a clean pass. `ADAPTIVE_ENABLED=false` disables the adaptive arm outright: chat requests that select it and evaluations that include it are rejected with HTTP 400.

The chat selector and the evaluation screen both expose all seven arms. See [Calibration](docs/calibration.md) for the dev-only adaptive thresholds.

## Evaluation budget

- Default cap: `EVALUATION_DEFAULT_MAX_REQUESTS=60`.
- Hard ceiling: `EVALUATION_HARD_MAX_REQUESTS=300`; per-run `max_requests` cannot raise it.
- Precedence: per-run `max_requests` > environment default > config default (`experiments/config.json`). `EVALUATION_DEFAULT_MAX_REQUESTS` is optional: when unset, the committed config value supplies the default.
- Before a run starts, `cases × arms × replicates` is checked against the cap. Over-cap runs are rejected with HTTP 400 and a message naming both remedies: reduce sample/arms/replicates, or explicitly raise `max_requests` up to the ceiling.
- If the cap is reached mid-run, new calls stop and the run is marked `status="aborted_budget"`, `partial=true`, with `_meta.partial/completed/target`. Its partial rows are kept for audit, but **headline comparisons exclude partial runs by default**: the HTML comparison report lists them in an appendix and `/api/evaluations/compare` returns them under `excluded_partial_runs`. Pass `include_partial=true` to include them.
- A full 42-case × 3-arm run needs up to **126** generation requests and therefore needs an explicit `max_requests` override (for example, `126`). Replicates multiply this count. The CLI exposes `--max-requests` (and `make experiment MAX_REQUESTS=…`); the UI has a budget selector that asks for a typed confirmation.
- The cap counts **actual generation attempts**, including provider retries, so a run with retries can overshoot the cap by the attempts of its final request before it stops.
- Nothing starts an evaluation automatically in Compose or CI.

Example request body for the full core run (illustrative; requires a working provider key):

```json
{
  "arms": ["off", "scanners", "full"],
  "sample_limit": 42,
  "seed": 42,
  "generation_model": "deepseek-v4.1-flash",
  "max_requests": 126
}
```

The reported dataset is pinned v2.0.0 (`data/evaluation/v2.jsonl`, 42 cases: 22 attacks and 20 benign). The approved dev set is calibration-only and never appears in reported arm comparisons. See [Calibration](docs/calibration.md).

## Pricing and cost

Cost accounting uses one committed snapshot, `data/pricing/opencode-go.json`, with provenance and an `integrity.sha256` over the canonical JSON excluding the integrity block. Request handling never fetches live prices.

- `notional_usd`: list-price equivalent used for cross-model comparison.
- `marginal_usd`: incremental USD. OpenCode Go is subscription-backed, so marginal USD is `0` inside the plan allowance; quota consumption is tracked separately.
- `priced=false`: never imputed. Unpriced models are listed separately and excluded from sums.
- `beam-free` has zero rates and is priced, not unknown. `kimi-k2.6` is unpriced at the recorded date.
- Report both notional and marginal cost. LLM Guard/Ollama have zero marginal USD; Cloudflare neuron usage is recorded but the per-token neuron rate for Llama Guard is unpublished, so guardrail cost remains unpriced.

Inspect at `GET /api/pricing`. Refresh manually: `uv run python scripts/refresh_pricing.py` (preview), then `--write` and commit the reviewed diff. The planned `make pricing-refresh` alias is not present in the current Makefile. See [Pricing](docs/pricing.md) and [`data/pricing/README.md`](data/pricing/README.md).

## Statistics and reports

Every report includes:

- Wilson score intervals for binomial proportions (attack success, false refusal, category slices).
- Seeded bootstrap percentile intervals for rates and paired deltas.
- Exact two-sided McNemar tests vs `off`; pairs include only cases present in both arms without errors.
- Holm–Bonferroni multiplicity control, applied separately to attack-success and false-refusal families.
- Approximate unpaired MDE (an upper bound for this paired design), n counts, and `exploratory` flags for category slices with n < 5.
- Sample size, seed, replicates, dataset/config/pricing hashes, provenance, gaps, and threats to validity.

Report endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/evaluations/runs/{id}/report.html` | Self-contained HTML report |
| `GET /api/evaluations/runs/{id}/report.json` | Machine-readable mirror of HTML data |
| `GET /api/evaluations/runs/{id}/export.csv` | **Full raw export**, including prompts/answers |
| `GET /api/reports/compare.html?run_ids=...` | Side-by-side HTML run comparison |
| `GET /api/evaluations/compare?run_ids=...` | JSON run comparison summary |
| `POST /api/compare` | Exploratory 1–4 variant chat calls; never stored as an evaluation run |

HTML and JSON reports exclude prompts, answers, and canary values by default. `?include_content=true` is an explicit opt-in: it is logged and stamps a visible banner, and content is HTML-escaped. CSV is intentionally different: it is the full raw result export and must be handled as sensitive. See [Comparison and statistics](docs/comparison.md).

## Security and privacy

- Auth is **off by default** for localhost, with a startup warning. Set `API_AUTH_TOKEN` (raw or `Bearer` form) to enable it. `/health`, `/metrics`, `/docs`, `/redoc`, and `/openapi.json` remain exempt.
- Rate limiting is **on** by default (`RATE_LIMIT_PER_MINUTE=60`), enforced by both a per-session and a per-host bucket; rotating `x-opencode-session` does not reset the host limit. HTTP 429 includes `Retry-After`.
- `MAX_BODY_BYTES=1000000` rejects oversized bodies with HTTP 413. Both the declared `Content-Length` and the bytes actually streamed are checked, so a chunked body without the header is still capped.
- `TRUSTED_HOSTS` is enforced when set: a request with a `Host` header outside the list is rejected with HTTP 400.
- Prompts are not logged by default (`LOG_PROMPTS=false`); JSON logging redacts secret-shaped values and `safe_prompt()` returns `[omitted]`.
- Upstream error bodies are not echoed to clients; errors are normalized.
- Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a minimal CSP, and an `X-Request-ID`.

See [Security and hardening](docs/security.md). Do not expose this stack publicly; it has no multi-tenant auth, TLS, or production hardening.

## Development and validation

```sh
make install      # uv sync --all-groups; frontend npm ci
make check        # lint, pytest, frontend build, docker compose config
make dev          # build and start API + frontend
make down         # stop/remove containers, keep volumes
```

CI validates tests, frontend, Compose config, pinning/audits, images, and a provider-free smoke test. It does not run evaluations or spend provider quota. See [Observability](docs/observability.md) for CI exporter details.

## Documentation

- [SLOs and burn alerts](docs/slo.md)
- [Observability](docs/observability.md)
- [Security and hardening](docs/security.md)
- [Adaptive calibration](docs/calibration.md)
- [Pricing snapshot](docs/pricing.md)
- [Comparison design and statistics](docs/comparison.md)
- [Evaluation dataset](data/evaluation/README.md)
- [Pricing snapshot README](data/pricing/README.md)

## Limitations

- Synthetic 42-case corpus, single canary/PII set, lexical success and refusal heuristics, temperature-0 non-determinism, provider/classifier drift, and snapshot notionality all limit generalization.
- Code-generation cases inspect text only; there is no execution sandbox.
- HTTP streaming is not implemented; the UI simulates typed output after the complete response.
- The OpenCode Go key used during documentation work is expired, so no live provider or guardrail-spending commands were run for verification.
- The evaluation screen and `security_eval.cli` expose all seven arms; `make experiment ARMS="off scanners full"` picks the set.
- All results and model answers depend on the specific provider, model, hardware, prompts, and pinned snapshots recorded in each run.
