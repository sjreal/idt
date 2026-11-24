# Security and hardening

This page describes the lab's local hardening. The stack is intended for `127.0.0.1` only. It is **not** a production service: no TLS, no multi-tenant authorization, no public exposure, and no customer SLA.

## Configuration

Defaults come from `src/llm_app/settings.py` and are surfaced in `.env.example`.

| Setting | Default | Effect |
| --- | --- | --- |
| `API_AUTH_TOKEN` | empty | Empty disables auth with a startup warning; set a token to require it |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-host and per-session token buckets; always enabled |
| `MAX_BODY_BYTES` | `1000000` | Bodies above this (declared or streamed) return 413 |
| `LOG_PROMPTS` | `false` | Prompts are omitted from logs unless explicitly enabled |
| `TRUSTED_HOSTS` | empty | When set, requests with a `Host` header outside the list are rejected with 400 |
| `CORS_ORIGINS` | localhost:5173 variants | Browser origins allowed by CORS; credentials disabled |

## Authentication

`check_auth()` accepts either the raw token or `Authorization: Bearer <token>` and compares in constant time. When `API_AUTH_TOKEN` is empty, startup logs:

> `API_AUTH_TOKEN is empty; authentication is disabled. Set it before exposing this service beyond localhost.`

These operational routes remain reachable without auth and without rate limiting:

- `/health`
- `/metrics`
- `/docs`
- `/redoc`
- `/openapi.json`

All other routes are protected when a token is configured. Auth failures increment `llm_lab_auth_failures_total`.

## Rate limiting

`TokenBucketLimiter(per_minute=max(1, RATE_LIMIT_PER_MINUTE))` uses a thread-safe bucket that refills linearly over 60 seconds. Rejections return HTTP 429 with `Retry-After` and increment `llm_lab_rate_limited_total`.

Two buckets are checked, and a request must pass both:

- **Per session** — the key is derived from client IP plus the `x-opencode-session` header; long components are hashed/truncated so prompt text cannot leak into the key. This keeps one noisy client from starving another on the same host.
- **Per host** — the key is the client IP alone. The caller controls the session header, so a session-only key could be reset on every request; the host bucket is what actually bounds the request rate.

Rate limiting applies even when auth is disabled. It is a local abuse guard, not a distributed quota system.

## Host validation

When `TRUSTED_HOSTS` is non-empty, Starlette's `TrustedHostMiddleware` rejects requests whose `Host` header is not in the list with HTTP 400. An empty value keeps the default local behaviour. This blocks DNS-rebinding style requests but is not a substitute for a reverse proxy or TLS termination.

## Request identifiers

Every response carries an `X-Request-ID` header. A caller-supplied `X-Request-ID` is echoed back when it matches `[A-Za-z0-9._:@+-]{1,64}`; anything else is replaced with a generated id, so a caller cannot inject control characters or unbounded values into headers or logs. The id is never derived from prompt or body content.

## Request size and headers

`enforce_body_limit()` rejects a declared `Content-Length` above `MAX_BODY_BYTES` with HTTP 413. The cap is enforced by a plain ASGI middleware that wraps the receive channel, so bytes are counted as the body streams in and an undeclared or chunked body is rejected once it exceeds the same cap. A malformed `Content-Length` is treated as unknown, so the streaming guard still applies.

Every response, including errors, gets:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'`
- `X-Request-ID`

## Logging and secrets

`src/llm_app/logging_config.py` installs a redacting JSON handler:

- Recursive scrubbing of log messages, arguments, and extra fields.
- Patterns for `sk-...`, `Bearer ...`, `api_key=...`, `*_TOKEN=...`, `*_SECRET=...`, `*_PASSWORD=...`, and long hex strings.
- `safe_prompt()` returns `[omitted]` unless `LOG_PROMPTS` is truthy.
- Exception text and stack information are redacted before emission.

When `LOG_PROMPTS=true` the prompt is written to the debug log through `safe_prompt()`. When it is false, the log line contains only the placeholder.

Enabling `LOG_PROMPTS` or trace/report content capture is an explicit local-debug decision and should be avoided when results may be shared. `TRACE_CAPTURE_CONTENT=true` stores the prompt and answer in the local JSONL trace buffer only; reports still exclude them unless a report explicitly opts in with `include_content=true`.

## Upstream and provider data

- Upstream error bodies are not echoed; normalized errors are returned instead.
- Cloudflare Workers AI credentials are read from `.env` and sent only to the configured Workers AI endpoint. Tokens are never included in error details.
- The default classifier backend is Cloudflare; prompts and generated answers are sent to Cloudflare when classifier arms run. Use `LLAMA_GUARD_BACKEND=ollama` to keep classification local.
- OpenCode Go requests use the configured key and a stable session header. Model choice is restricted to the 19 Chat Completions-compatible allowlist entries.
- Pricing is read from a committed snapshot; no live price scraping occurs in the request path. See [Pricing](pricing.md).

Guardrails fail closed: if an enabled scanner/classifier cannot run, input is not sent to the model or output is withheld.

## Evaluation artifacts

Evaluation runs persist `run.json` and `results.jsonl`, which include prompts and answers, in the local `evaluation_results` volume. Exports and reports have different disclosure levels:

- HTML/JSON reports exclude prompt/answer/canary content unless `include_content=true`, which is logged and bannered.
- `export.csv` is the full raw export and must be treated as sensitive.
- Comparison reports do not include per-case text.

See [Comparison and statistics](comparison.md) and the [main README](../README.md). `docker compose down --volumes` deletes evaluation history permanently.

## Scope

Keep all services bound to localhost. Do not add public ingress, shared credentials, or "just for testing" exposure. The lab has no multi-tenant auth model, audit pipeline, key rotation, TLS termination, or production incident process.
