# Observability

Observability is split into three planes. They are complementary; none is required to use the chat app.

| Plane | Tooling | Question it answers | Availability |
| --- | --- | --- | --- |
| Aggregate/ops | Prometheus + Grafana | Is the API healthy, slow, failing, falling back, or burning SLO budget? | Opt-in via `observability` profile |
| Trace | OpenTelemetry spans + optional Phoenix | Which generation, guardrail stage, retry, or fallback caused this request to fail or slow down? | Export opt-in; Phoenix optional |
| Evidence | Evaluation runs + JSONL/CSV/HTML reports | What happened for each `(case, arm, model, replicate)`, with hashes and statistics? | Always available through API/results volume |

There is no Langfuse integration. The local JSONL trace buffer works even when Phoenix is stopped.

## Commands

```sh
make dev              # API + frontend only
make observability-up # adds Prometheus, Grafana, CI exporter, Phoenix
make observability-down # stops Grafana, Prometheus, CI exporter, Phoenix
```

`make observability-up` and `make observability-down` use both `docker-compose.yml` and `docker-compose.observability.yml`. The override points the app at the Phoenix OTLP/HTTP collector (`http://phoenix:6006/v1/traces`), so enabling the profile wires the trace plane end to end. The base file alone leaves `OTEL_EXPORTER_OTLP_ENDPOINT` empty, so a plain `docker compose up` produces zero exporter traffic and does not require the trace plane.

Phoenix is a service under the `observability` profile; the app does not require it. To export spans without the override, set `OTEL_EXPORTER_OTLP_ENDPOINT` yourself (for example `http://localhost:6006/v1/traces`) and recreate the API.

## Local surfaces

| Service | URL | Credential / note |
| --- | --- | --- |
| API metrics | <http://localhost:8000/metrics> | Prometheus text exposition; no prompt/answer labels |
| Prometheus | <http://localhost:9090> | Alerts at `/alerts`; 15d retention |
| Grafana | <http://localhost:3001> | `admin` / `local-only-change-me` from `.env.example` |
| CI exporter | <http://localhost:9101/metrics> | Read-only GitHub Actions history for the configured repo/branch |
| Phoenix | <http://localhost:6006> | UI and OTLP/HTTP collector share port 6006 |
| Observability config | <http://localhost:8000/api/observability/config> | Reports tracing/content-capture state and UI links |
| Local trace buffer | `GET /api/traces`, `GET /api/traces/{trace_id}` | Per-day JSONL with retention and byte cap |

All ports are bound to `127.0.0.1`.

## Metrics

`src/llm_app/metrics.py` exports counters/histograms for:

- HTTP request count and duration by method, route, and status code.
- Generation requests, duration, retries, and prompt/completion tokens by backend/model.
- Guardrail checks, check duration by arm/stage/backend, prefilter decisions, adaptive decisions, classifier invocations.
- Fallback totals, circuit state, auth failures, rate-limit rejections.
- Evaluation runs/cases by status, arm, and kind.
- Notional/marginal generation cost and provider allowance gauges.

Metric labels are bounded and omit prompts, answers, run IDs, and credentials. The fallback reason label is a fixed enum (`http_503`, `http_4xx`, `timeout`, `network`, …) rather than the raw provider message, so a flapping provider cannot explode metric cardinality. `/metrics` is not instrumented by the HTTP middleware and is excluded from chat availability SLOs.

A guardrail check is only counted when a stage actually ran. The `off` arm contributes no `llm_lab_guardrail_checks` samples, so baseline rates are not diluted by phantom checks.

## Tracing

`src/llm_app/telemetry.py` uses manual spans with OTel GenAI semantic conventions where available (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) and project attributes for arm, stage, outcome, blocked-by, latency, and attempts. Spans are exported over OTLP/HTTP by `OTLPSpanExporter` when:

1. `OTEL_EXPORTER_OTLP_ENDPOINT` is non-empty, and
2. the OpenTelemetry packages are importable.

Setup failures are logged and never break a request. With no endpoint, a no-op tracer is used and the app continues normally.

The root span is attached as the current span for the duration of the request, so the input, generation, and output spans are nested under it and share one trace id. That id is read from the span itself, which means the `trace_id` in the API response, in the local JSONL buffer, and on every exported span is the same value — the local trace viewer and Phoenix can be joined on it.

Content capture is off by default:

```dotenv
OTEL_CAPTURE_CONTENT=false
TRACE_CAPTURE_CONTENT=false
```

Sensitive text-like span attributes are replaced with `[redacted]` unless capture is explicitly enabled; numeric token/latency values are preserved. Enabling capture is a deliberate privacy decision and should be limited to local debugging.

- `OTEL_CAPTURE_CONTENT=true` adds `gen_ai.prompt` and `gen_ai.completion` to the generation span.
- `TRACE_CAPTURE_CONTENT=true` stores the prompt and answer in the local JSONL buffer only. Reports still exclude them unless a report opts in with `include_content=true`.

## Local trace buffer and UI

Every completed or failed chat/evaluation call appends a redacted record to `TRACE_DIR` (`traces/` by default) as per-day JSONL. It includes trace ID, route, backend/model, arm, status, latency, stage spans, verdict, cost, and error, but no prompt/answer content unless capture is enabled.

- `TRACE_RETENTION_DAYS` defaults to 7.
- `TRACE_MAX_BYTES` defaults to 50,000,000; oldest day files are removed first.
- `/api/traces?limit=...` lists recent records; `/api/traces/{trace_id}` retrieves one.
- The UI trace view can open a trace directly from a chat message or evaluation row.

Storage failures are counted and logged but never fail generation.

## SLOs and alerts

See [SLOs and burn alerts](slo.md). Targets are **provisional** until a 7-day baseline is collected and reviewed; they are engineering targets for a localhost demo, not customer SLAs.

Prometheus rules in `observability/alerts.yml` cover target down, sustained 5xx, repeated provider failures, chat availability fast/slow burn, guardrail unavailability, evaluation aborts, extended circuit opens, and high fallback rate. Notifications require an Alertmanager/contact point; none is configured by default.

## Validation without services

`make check` runs Ruff, pytest, frontend build, and `docker compose config --quiet` for both the base and observability compose files. It does not call a model provider, start an evaluation, or require Phoenix. The GitHub Actions smoke test uses an empty OpenCode Go key, checks health/metrics/summary and the frontend proxy, and makes no chat or evaluation calls.

For dashboard activity without provider spend, call non-generation endpoints such as `GET /api/evaluations/summary` and wait for a Prometheus scrape. Generation, guardrail, and evaluation panels require explicitly initiated work that can consume provider quota.
