# Service-level objectives (SLOs)

> **These are engineering targets for a localhost demo, not customer SLAs.**
> All numbers below are **PROVISIONAL** until a **7-day baseline** has been
> collected and reviewed. Do not treat a breach as a contractual failure.

This page defines the service-level indicators (SLIs) recorded in
`observability/rules/slo.yml` and alerted on in `observability/alerts.yml`.
Prometheus evaluates the recording rules every 30s and retains 15d of data,
which is enough for the 7d rolling windows.

Deployment note: `observability/prometheus.yml` loads
`/etc/prometheus/rules/slo.yml`, so the Prometheus container must also mount
`./observability/rules` at `/etc/prometheus/rules` (read-only) alongside the
existing `alerts.yml` mount.

## Targets

| SLI | Definition | Provisional target | Window |
| --- | --- | --- | --- |
| Chat availability | Non-5xx responses on `POST /api/chat` and `POST /v1/chat/completions`; 5xx / 502 / 503 are failures, 4xx are successful requests | >= 99.0% | 7d rolling |
| Chat latency (p95) | End-to-end `histogram_quantile(0.95, ...)` of `llm_lab_http_request_duration_seconds_bucket` on chat endpoints | <= 10s (OpenCode/Go backend); local/Ollama expected <= 3s | 5m rate |
| Guardrail latency (p95, cloudflare) | p95 of `llm_lab_guardrail_check_duration_seconds_bucket` where `backend="cloudflare"` | <= 1.5s | 5m rate |
| Guardrail latency (p95, scanners) | p95 of the same histogram where `backend=~"llm-guard\|llm_guard\|scanners"` | <= 300ms | 5m rate |
| Eval case completion | Cases with outcomes other than `error` / `failed` / `aborted`, divided by all evaluation cases | >= 99% | 1h (per run) |
| Eval run completion | Runs with `status=~"completed\|completed_with_errors"` divided by terminal runs (also `failed`, `aborted_budget`) | >= 99% (diagnostic) | 24h |

`chat_latency` uses a **single 10s target** in the recording rules. The
duration histogram has no backend label, so the stricter 3s local expectation
is documented here and in [Grafana](#grafana) but is not encoded as a separate
recording rule. Availability uses the `status_code` label emitted by
`src/llm_app/metrics.py` (not `status`).

## Measurement windows and burn alerts

| Alert | Error budget burned | Long window | Short window | Severity | Pending |
| --- | --- | --- | --- | --- | --- |
| `ChatAvailabilityFastBurn` | 14.4x | 1h | 5m | `page` | 2m |
| `ChatAvailabilitySlowBurn` | 6x | 6h | 30m | `ticket` | 15m |
| `GuardrailUnavailable` | >50% failed checks, or no guardrail checks scraped | 5m rate / 10m absence | - | `page` | 10m |
| `EvaluationRunAborted` | any `aborted_budget` / `failed` run | 10m | - | `ticket` | immediate |
| `CircuitOpenExtended` | any circuit open | 2m | - | `page` | 2m |
| `FallbackRateHigh` | >10% of generation requests fall back | 5m rate + 15m pending | - | `ticket` | 15m |

The error budget for 99.0% availability is **1%** of chat requests over 7d.
Burn rate = observed error ratio / 0.01. A burn rate of 14.4x would spend the
entire 7d budget in roughly 12 hours; 6x would spend it in about 28 hours.
Alerts require both a long and a short window to agree, which reduces noise
from a single failed request. A low-traffic guard (`slo:chat_requests:rate5m >
0.005` req/s) suppresses burn alerts when there is not enough traffic to
measure an SLI.

### Exclusions

- `/metrics` is not instrumented by the HTTP middleware and is never counted.
- `/health` and other health/readiness probes are excluded by the endpoint
  match on `/api/chat` and `/v1/chat/completions`.
- Client errors (4xx, including 429 rate limits and blocked-input responses)
  are **not** availability errors; they remain in the denominator and count as
  successful requests.
- Requests to unmatched routes and non-chat endpoints (for example
  `/api/evaluations/*`, `/v1/models`, `/api/pricing`) are tracked on the
  Grafana dashboard but do not contribute to the chat SLO.

## Error-budget policy

1. While the budget remains, ship changes normally; use the burn alerts to
   catch regressions early.
2. At **50% of the 7d budget consumed**, open a tracking ticket and review the
   Grafana latency/error panels and Phoenix traces for the likely cause.
3. At **100% of the budget consumed** (or a confirmed `page` burn alert),
   stop non-essential changes to the chat path, prioritise reliability fixes,
   and record the incident and remediation in the ticket.
4. After recovery, confirm the 5m/1h burn rates are back below 1x before
   resuming normal changes. Keep the provisional targets under review; replace
   them with values informed by the 7d baseline and re-approve this document.

### What to do when the budget is exhausted

- Check `ChatAvailabilityFastBurn` / `ChatAvailabilitySlowBurn` in Prometheus
  (`http://127.0.0.1:9090/alerts`) and the panel links below.
- Use Phoenix traces to identify which span (generation, guardrail, fallback,
  or circuit breaker) is failing, then mitigate (for example, disable a bad
  backend or reduce guardrail level) before root-cause work.
- Note the burn rate, affected endpoints/backends, and mitigation in the
  incident ticket; update the runbook placeholder in the alert annotation.
- Revisit the target itself: a persistent breach with no clear regression may
  mean the provisional target is wrong, not that the service is broken.

## Panels and links

### Grafana

Dashboard: [LLM Guardrail Lab — Live](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live)
(dashboard UID `llm-guardrail-lab-live`; Grafana is exposed on port 3001).

- [API request rate](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=1)
- [HTTP server errors](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=2)
- [API latency p50/p95/p99](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=3)
- [Guardrail check rate](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=7)
- [Guardrail latency by mode and stage (p95)](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=16)
- [Evaluation case completion rate](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=9)
- [Evaluation runs by terminal status](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=18)
- [Firing Prometheus alerts](http://127.0.0.1:3001/d/llm-guardrail-lab-live/llm-guardrail-lab-live?viewPanel=24)

Prometheus: `http://127.0.0.1:9090` — query the `slo:*` recording rules
directly, or open `/alerts` for firing rules.

### Phoenix

Tracing is optional and only enabled when `OTEL_EXPORTER_OTLP_ENDPOINT` is
set (see `src/llm_app/telemetry.py`). With tracing enabled, open the
[Phoenix UI](http://localhost:6006) (default `PHOENIX_UI_URL`) and filter the
trace view to service `llm-guardrail-lab` (default `OTEL_SERVICE_NAME`) to
correlate a slow or failed chat request with its generation, guardrail, and
fallback spans. The API `/health` response also reports the configured
tracing status and Phoenix URL.
