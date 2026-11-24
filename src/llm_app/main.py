"""FastAPI application exposing app-native and OpenAI-compatible chat APIs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from uuid import uuid4
from weakref import WeakKeyDictionary

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from llm_app import telemetry
from llm_app.backends.base import ChatBackend
from llm_app.backends.ollama import ModelServiceError, OllamaBackend
from llm_app.backends.opencode_go import OpenCodeGoBackend
from llm_app.guardrails import GuardrailPipeline, GuardrailUnavailable, build_verdict
from llm_app.logging_config import configure_logging, safe_prompt
from llm_app.metrics import (
    AUTH_FAILURES,
    CIRCUIT_OPEN,
    FALLBACK_TOTAL,
    GENERATION_COST_USD,
    GENERATION_REQUEST_DURATION_SECONDS,
    GENERATION_REQUESTS,
    GENERATION_TOKENS,
    GUARDRAIL_CHECK_DURATION_SECONDS,
    GUARDRAIL_CHECKS,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS,
    RATE_LIMITED,
    TRACE_BUFFER_DROPPED,
)
from llm_app.pricing import (
    PricingError,
    catalog_document,
    generation_cost,
    get_catalog,
)
from llm_app.routing import (
    CircuitBreaker,
    ModelRouter,
    NonRetryableError,
    RetryableError,
    RoutingDecision,
)
from llm_app.schemas import (
    ChatRequest,
    ChatResponse,
    ChatUsage,
    CompareRequest,
    CostInfo,
    EvaluationStartRequest,
    HealthResponse,
)
from llm_app.security import (
    SECURITY_HEADERS,
    BodySizeLimitMiddleware,
    TokenBucketLimiter,
    check_auth,
    enforce_body_limit,
    host_rate_limit_key,
    parse_content_length,
    rate_limit_key,
)
from llm_app.settings import get_settings
from llm_app.trace_store import TraceStore
from security_eval.report import (
    ReportOptions,
    render_comparison_report,
    render_report_json,
    render_run_report,
)
from security_eval.runner import (
    EvaluationAlreadyRunning,
    EvaluationBudgetExceeded,
    EvaluationRunManager,
)

settings = get_settings()
configure_logging(level="INFO", log_prompts=settings.log_prompts)
logger = logging.getLogger(__name__)

telemetry.configure(
    settings.otel_service_name,
    settings.otel_exporter_endpoint or None,
    capture_content=settings.otel_capture_content or settings.trace_capture_content,
)

TRACE_STORE = TraceStore(
    Path(settings.trace_dir),
    retention_days=settings.trace_retention_days,
    max_bytes=settings.trace_max_bytes,
)
RATE_LIMITER = TokenBucketLimiter(per_minute=max(1, settings.rate_limit_per_minute))
CIRCUIT = CircuitBreaker(
    failure_threshold=settings.circuit_failure_threshold,
    cooldown_seconds=settings.circuit_cooldown_seconds,
)

# Bound concurrent provider calls. Semaphores are per event loop so TestClient
# instances (each with its own portal loop) do not share a bound primitive.
_GENERATION_SEMAPHORES: WeakKeyDictionary[Any, asyncio.Semaphore] = WeakKeyDictionary()
_SEMAPHORE_LOCK = threading.Lock()


def _generation_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _SEMAPHORE_LOCK:
        semaphore = _GENERATION_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(1, settings.generation_concurrency))
            _GENERATION_SEMAPHORES[loop] = semaphore
        return semaphore

if not settings.api_auth_token:
    logger.warning(
        "API_AUTH_TOKEN is empty; authentication is disabled. Set it before exposing "
        "this service beyond localhost."
    )

app = FastAPI(
    title="LLM Guardrail Lab API",
    description="Local LLM chat API and OpenAI-compatible endpoint for evaluation tools.",
    version="0.2.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "x-opencode-session"],
)
# Enforce the request body cap while the body streams, not only on the
# declared Content-Length header. Added as plain ASGI middleware so the byte
# counter wraps the receive channel the application actually reads from.
app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=lambda: settings.max_body_bytes,
)
# Host header validation is opt-in: an empty TRUSTED_HOSTS keeps the default
# local behaviour, while a non-empty list blocks DNS-rebinding style requests.
if settings.trusted_hosts:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.trusted_hosts),
    )

# Routes that stay reachable without auth/rate limiting for local ops and docs.
_UNAUTHENTICATED_PATHS = {
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
}

# An inbound request id is only echoed back when it matches this pattern, so a
# caller cannot inject control characters or a very long value into headers.
_SAFE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:@+-]{1,64}")


@app.middleware("http")
async def collect_http_metrics(request, call_next):
    started_at = perf_counter()
    status_code = 500
    path = request.url.path

    # Security headers on every response, including errors.
    try:
        enforce_body_limit(
            parse_content_length(request.headers.get("content-length")),
            settings.max_body_bytes,
        )
        if path not in _UNAUTHENTICATED_PATHS and settings.api_auth_token:
            try:
                check_auth(
                    None,
                    request.headers.get("authorization"),
                    configured_token=settings.api_auth_token,
                )
            except HTTPException:
                AUTH_FAILURES.labels(path).inc()
                raise
        if path not in _UNAUTHENTICATED_PATHS:
            client_host = request.client.host if request.client else None
            session_id = request.headers.get("x-opencode-session")
            # Two buckets apply. The session bucket keeps one noisy client from
            # starving another on the same host. The host bucket is the real
            # limit: without it a caller could rotate x-opencode-session values
            # and get a fresh allowance on every request.
            session_allowed, session_retry_after = RATE_LIMITER.allow(
                rate_limit_key(client_host, session_id)
            )
            host_allowed, host_retry_after = RATE_LIMITER.allow(
                host_rate_limit_key(client_host)
            )
            if not (session_allowed and host_allowed):
                RATE_LIMITED.labels(path).inc()
                retry_after = max(session_retry_after, host_retry_after)
                return Response(
                    content="Rate limit exceeded.",
                    status_code=429,
                    headers={"Retry-After": str(retry_after), **SECURITY_HEADERS},
                )
        response = await call_next(request)
        status_code = response.status_code
        return response
    except HTTPException as exc:
        status_code = exc.status_code
        return Response(
            content=exc.detail if isinstance(exc.detail, str) else "Request rejected.",
            status_code=exc.status_code,
            headers={**SECURITY_HEADERS, **(getattr(exc, "headers", None) or {})},
        )
    finally:
        if path != "/metrics":
            route = request.scope.get("route")
            endpoint = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, endpoint, str(status_code)).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(request.method, endpoint).observe(
                perf_counter() - started_at
            )


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


@app.middleware("http")
async def add_request_id(request, call_next):
    """Attach a stable request id to every response and log line.

    The id is taken from an inbound ``X-Request-ID`` when it looks safe, so a
    caller can correlate its own logs with the server's, and generated
    otherwise. It is never derived from prompt or body content.
    """
    inbound = request.headers.get("x-request-id", "")
    request_id = inbound.strip()[:64] if _SAFE_REQUEST_ID_RE.fullmatch(inbound.strip()) else ""
    if not request_id:
        request_id = uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers.setdefault("X-Request-ID", request_id)
    return response


@lru_cache
def get_backend() -> ChatBackend:
    if settings.llm_backend == "ollama":
        return OllamaBackend(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.request_timeout_seconds,
            excluded_models=(settings.llama_guard_model,),
        )
    if settings.llm_backend == "opencode-go":
        return OpenCodeGoBackend(
            base_url=settings.opencode_go_base_url,
            model=settings.opencode_go_model,
            api_key=settings.opencode_go_api_key,
            timeout_seconds=settings.request_timeout_seconds,
        )
    raise RuntimeError(f"Unsupported LLM_BACKEND: {settings.llm_backend}")


BackendDependency = Annotated[ChatBackend, Depends(get_backend)]


@lru_cache
def get_guardrail_pipeline() -> GuardrailPipeline:
    return GuardrailPipeline(settings)


GuardrailDependency = Annotated[GuardrailPipeline, Depends(get_guardrail_pipeline)]


@lru_cache
def get_evaluation_manager() -> EvaluationRunManager:
    return EvaluationRunManager(Path(settings.evaluation_results_dir))


EvalManagerDependency = Annotated[EvaluationRunManager, Depends(get_evaluation_manager)]


@lru_cache
def get_model_router() -> ModelRouter | None:
    if settings.llm_backend != "opencode-go" or not settings.opencode_go_fallback_models:
        return None
    primary = settings.opencode_go_model
    chain = [primary, *[m for m in settings.opencode_go_fallback_models if m != primary]]
    router = ModelRouter(chain, circuit=CIRCUIT)
    router.validate_chain(list(OpenCodeGoBackend.chat_completion_models))
    return router


def require_supported_model(request: ChatRequest, backend: ChatBackend) -> None:
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported yet. Set stream to false.",
        )


def guardrail_metric_backend(level: str) -> str:
    if level in {"full", "input", "output"} or level == "adaptive":
        return settings.llama_guard_backend
    if level == "scanners":
        return "llm-guard"
    return "none"


def build_cost_info(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> CostInfo | None:
    """Attach list-price and marginal cost without letting pricing break generation."""
    try:
        breakdown = generation_cost(get_catalog(), model, prompt_tokens, completion_tokens)
    except PricingError as exc:
        logger.warning("Pricing catalog unavailable: %s", exc)
        return None
    if breakdown["priced"]:
        GENERATION_COST_USD.labels(provider, model, "notional").inc(
            breakdown["notional_usd"] or 0.0
        )
        GENERATION_COST_USD.labels(provider, model, "marginal").inc(
            breakdown["marginal_usd"] or 0.0
        )
    return CostInfo(
        priced=bool(breakdown["priced"]),
        notional_usd=breakdown["notional_usd"],
        marginal_usd=breakdown["marginal_usd"],
        currency=str(breakdown["currency"]),
        billing_mode=str(breakdown["billing_mode"]),
        pricing_sha256=breakdown["pricing_sha256"],
        unpriced_reason=breakdown["unpriced_reason"],
    )


def _routing_error(exc: ModelServiceError) -> Exception:
    """Translate provider errors into routing classes (conservative by default)."""
    message = str(exc).lower()
    retryable_markers = (
        "502", "503", "504", "429", "500", "520", "521", "522", "523", "524",
        "timeout", "timed out", "cannot reach", "invalid response", "after ",
    )
    if any(marker in message for marker in retryable_markers):
        return RetryableError(str(exc))
    return NonRetryableError(str(exc))


def _record_trace(
    *,
    trace_id: str,
    kind: str,
    route: str,
    session_id: str | None,
    model: str,
    backend: str,
    arm: str,
    status: str,
    latency_ms: float,
    verdict: object | None,
    cost: object | None,
    error: str | None,
    usage: object | None = None,
    content: dict[str, str] | None = None,
) -> None:
    """Append a redacted trace record; never let trace storage fail a request."""
    spans: list[dict[str, Any]] = []
    if verdict is not None:
        stages = getattr(verdict, "stages", []) or []
        offset = 0.0
        generation_inserted = False
        guardrail_latency = float(getattr(verdict, "latency_ms", 0) or 0)
        generation_latency = max(0.0, latency_ms - guardrail_latency)

        def generation_span() -> dict[str, Any]:
            return {
                "name": "gen_ai.chat",
                "start_offset_ms": round(offset, 2),
                "duration_ms": round(generation_latency, 2),
                "attributes": telemetry.safe_attributes(
                    **{
                        telemetry.ATTR_MODEL: model,
                        telemetry.ATTR_TRACE_ID: trace_id,
                    }
                ),
            }

        for stage in stages:
            stage_name = str(getattr(stage, "stage", "unknown"))
            if not generation_inserted and stage_name == "classifier_output":
                spans.append(generation_span())
                generation_inserted = True
            duration = float(getattr(stage, "latency_ms", 0) or 0)
            spans.append(
                {
                    "name": f"guardrail.{stage_name}",
                    "start_offset_ms": round(offset, 2),
                    "duration_ms": round(duration, 2),
                    "attributes": telemetry.safe_attributes(
                        app_stage=stage_name,
                        app_backend=getattr(stage, "backend", None),
                        app_outcome=getattr(stage, "outcome", None),
                    ),
                }
            )
            offset += duration
        if not generation_inserted:
            spans.append(generation_span())
    record = {
        "trace_id": trace_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "request_id": f"req-{uuid4().hex}",
        "session_id": session_id,
        "route": route,
        "model": model,
        "backend": backend,
        "arm": arm,
        "status": status,
        "total_latency_ms": round(latency_ms, 2),
        "spans": spans,
        "verdict": verdict.model_dump() if verdict is not None else None,
        "cost": cost.model_dump() if cost is not None and hasattr(cost, "model_dump") else cost,
        "usage": usage.model_dump() if usage is not None and hasattr(usage, "model_dump") else None,
        "error": error,
    }
    if settings.trace_capture_content and content:
        # Content capture is opt-in and off by default. When it is enabled the
        # prompt/answer text is stored in the local buffer only; it is still
        # excluded from reports unless the report opts in as well.
        record["content"] = {
            key: value for key, value in content.items() if value
        }
    try:
        TRACE_STORE.append(record)
    except Exception as exc:
        TRACE_BUFFER_DROPPED.labels("write_error").inc()
        logger.warning("Could not write local trace record: %s", exc)


def _trace_content(
    request: ChatRequest, response: ChatResponse | None
) -> dict[str, str] | None:
    """Return prompt/answer text for the local trace buffer, when enabled.

    Content capture is off by default. When it is switched on the text is
    written to the local JSONL buffer only; it is still excluded from reports
    unless a report explicitly opts in.
    """
    if not settings.trace_capture_content:
        return None
    prompt = request.messages[-1].content if request.messages else ""
    return {"prompt": prompt, "answer": response.answer if response else ""}


async def complete_chat(
    request: ChatRequest,
    backend: ChatBackend,
    guardrails: GuardrailPipeline,
    session_id: str | None = None,
    *,
    allow_fallback: bool = True,
    trace_kind: str = "chat",
) -> tuple[ChatResponse, str]:
    require_supported_model(request, backend)
    started = perf_counter()
    tracer = telemetry.get_tracer()
    root_attributes = telemetry.safe_attributes(
        **{
            telemetry.ATTR_MODEL: request.model or backend.model,
            telemetry.ATTR_ARM: request.guardrail_level or settings.guardrail_level,
            "app.backend": backend.provider,
            "http.route": "/api/chat",
        }
    )
    root_span = tracer.start_span("chat.request", attributes=root_attributes)
    # Read the trace id from the span itself so the local trace buffer, the
    # response, and the exported OTLP spans all agree on one identifier.
    trace_id = telemetry.span_trace_id(root_span) or telemetry.new_trace_id()
    span_token = telemetry.attach_span(root_span)
    response: ChatResponse | None = None
    error_text: str | None = None
    try:
        response, completion_id = await _complete_chat_inner(
            request,
            backend,
            guardrails,
            session_id,
            trace_id=trace_id,
            span=root_span,
            tracer=tracer,
            allow_fallback=allow_fallback,
        )
        return response, completion_id
    except HTTPException as exc:
        error_text = f"HTTP {exc.status_code}"
        root_span.record_exception(exc)
        raise
    except Exception as exc:
        error_text = type(exc).__name__
        root_span.record_exception(exc)
        raise
    finally:
        telemetry.detach_span(span_token)
        root_span.set_attribute(
            telemetry.ATTR_LATENCY_MS, round((perf_counter() - started) * 1000, 2)
        )
        root_span.set_attribute("app.outcome", error_text or "success")
        root_span.set_attribute(telemetry.ATTR_TRACE_ID, trace_id)
        root_span.end()
        _record_trace(
            trace_id=trace_id,
            kind=trace_kind,
            route="/api/chat",
            session_id=session_id,
            model=response.model if response else (request.model or backend.model),
            backend=backend.provider,
            arm=request.guardrail_level or settings.guardrail_level,
            status=error_text or "success",
            latency_ms=round((perf_counter() - started) * 1000, 2),
            verdict=response.guardrail if response else None,
            cost=response.cost if response else None,
            error=error_text,
            usage=response.usage if response else None,
            content=_trace_content(request, response),
        )


async def _complete_chat_inner(
    request: ChatRequest,
    backend: ChatBackend,
    guardrails: GuardrailPipeline,
    session_id: str | None,
    *,
    trace_id: str,
    span: Any,
    tracer: Any,
    allow_fallback: bool,
) -> tuple[ChatResponse, str]:
    try:
        available_models = await backend.list_models()
    except ModelServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    selected_model = request.model or backend.model
    if selected_model not in available_models:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{selected_model}' is not available from the configured provider.",
        )

    guardrail_level = request.guardrail_level or settings.guardrail_level
    if guardrail_level == "adaptive" and not settings.adaptive_enabled:
        raise HTTPException(
            status_code=400,
            detail="The adaptive arm is disabled (ADAPTIVE_ENABLED=false).",
        )
    start = perf_counter()
    input_span = tracer.start_span(
        "guardrail.input",
        attributes=telemetry.safe_attributes(
            **{
                telemetry.ATTR_ARM: guardrail_level,
                telemetry.ATTR_STAGE: "input",
                telemetry.ATTR_TRACE_ID: trace_id,
            }
        ),
    )
    input_guardrail_backend = guardrail_metric_backend(guardrail_level)
    input_guardrail_started_at = perf_counter()
    try:
        input_result = await guardrails.inspect_input(
            [message.model_dump() for message in request.messages], guardrail_level
        )
    except GuardrailUnavailable as exc:
        GUARDRAIL_CHECK_DURATION_SECONDS.labels(
            guardrail_level, "input", input_guardrail_backend
        ).observe(perf_counter() - input_guardrail_started_at)
        GUARDRAIL_CHECKS.labels(guardrail_level, "input", input_guardrail_backend, "error").inc()
        input_span.record_exception(exc)
        input_span.end()
        logger.warning("Input guardrail unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Only count a check when a stage actually ran. The "off" arm (and an
    # adaptive arm that passed the prefilter) runs no input stage, so counting
    # it would add phantom guardrail checks to every rate.
    if input_result.stages:
        GUARDRAIL_CHECK_DURATION_SECONDS.labels(
            guardrail_level, "input", input_guardrail_backend
        ).observe(perf_counter() - input_guardrail_started_at)
        GUARDRAIL_CHECKS.labels(
            guardrail_level, "input", input_guardrail_backend, input_result.outcome
        ).inc()
    input_span.set_attribute(telemetry.ATTR_OUTCOME, input_result.outcome)
    input_span.set_attribute(telemetry.ATTR_BLOCKED_BY, input_result.blocked_by)
    input_span.set_attribute(telemetry.ATTR_LATENCY_MS, input_result.latency_ms)
    input_span.end()

    if input_result.outcome == "blocked":
        verdict = build_verdict(guardrail_level, input_result)
        response = ChatResponse(
            answer="This message was blocked by the input safety checks.",
            model=selected_model,
            latency_ms=round((perf_counter() - start) * 1000, 2),
            usage=ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            guardrail=verdict,
            cost=build_cost_info(backend.provider, selected_model, 0, 0),
            trace_id=trace_id,
        )
        return response, f"chatcmpl-{uuid4().hex}"

    latest_prompt = input_result.messages[-1]["content"]
    if settings.log_prompts:
        # Prompt text is only ever logged when LOG_PROMPTS is explicitly on.
        logger.debug(
            "chat prompt: %s", safe_prompt(latest_prompt, settings.log_prompts)
        )
    generation_started_at = perf_counter()
    generation_outcome = "error"
    result: dict[str, Any]
    decision = RoutingDecision(
        model=selected_model,
        attempted_models=[selected_model],
        fallback_from=None,
        reason=None,
        fallback_used=False,
    )
    router = get_model_router()

    async def attempt(model: str) -> dict[str, Any]:
        try:
            return await backend.chat(
                input_result.messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                seed=request.seed,
                model=model,
                session_id=session_id,
            )
        except ModelServiceError as exc:
            raise _routing_error(exc) from exc

    gen_span = tracer.start_span(
        "gen_ai.chat",
        attributes=telemetry.safe_attributes(
            **{
                "gen_ai.system": backend.provider,
                telemetry.ATTR_MODEL: selected_model,
                telemetry.ATTR_TRACE_ID: trace_id,
            }
        ),
    )
    try:
        async with _generation_semaphore():
            if router is not None and allow_fallback:
                result, decision = await router.run(attempt, allow_fallback=True)
                for from_model, to_model, reason in (
                    (decision.fallback_from, decision.model, decision.reason_code),
                ):
                    if from_model and to_model:
                        FALLBACK_TOTAL.labels(from_model, to_model, reason or "unknown").inc()
                for model in router.chain:
                    CIRCUIT_OPEN.labels(model).set(1 if CIRCUIT.is_open(model) else 0)
            else:
                result = await attempt(selected_model)
        generation_outcome = "success"
    except (RetryableError, NonRetryableError) as exc:
        gen_span.record_exception(exc)
        gen_span.end()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        GENERATION_REQUESTS.labels(backend.provider, decision.model, generation_outcome).inc()
        GENERATION_REQUEST_DURATION_SECONDS.labels(backend.provider, decision.model).observe(
            perf_counter() - generation_started_at
        )

    gen_span.set_attribute(telemetry.ATTR_USAGE_INPUT, int(result["prompt_tokens"]))
    gen_span.set_attribute(telemetry.ATTR_USAGE_OUTPUT, int(result["completion_tokens"]))
    gen_span.set_attribute(telemetry.ATTR_ATTEMPTS, len(decision.attempted_models))
    gen_span.set_attribute(telemetry.ATTR_MODEL, decision.model)
    if settings.otel_capture_content:
        # safe_attributes keeps the text only when OTEL_CAPTURE_CONTENT is on;
        # otherwise it is replaced with "[redacted]".
        gen_span.set_attributes(
            telemetry.safe_attributes(
                **{
                    "gen_ai.prompt": latest_prompt,
                    "gen_ai.completion": result["content"],
                }
            )
        )
    gen_span.end()

    GENERATION_TOKENS.labels(backend.provider, decision.model, "prompt").inc(
        int(result["prompt_tokens"])
    )
    GENERATION_TOKENS.labels(backend.provider, decision.model, "completion").inc(
        int(result["completion_tokens"])
    )

    answer = result["content"]
    output_result = None
    output_span = tracer.start_span(
        "guardrail.output",
        attributes=telemetry.safe_attributes(
            **{
                telemetry.ATTR_ARM: guardrail_level,
                telemetry.ATTR_STAGE: "output",
                telemetry.ATTR_TRACE_ID: trace_id,
            }
        ),
    )
    output_guardrail_backend = guardrail_metric_backend(guardrail_level)
    output_guardrail_started_at = perf_counter()
    try:
        # GuardrailPipeline accepts the input result so adaptive can resolve its output
        # stage; older/test doubles expose the original three-argument signature.
        if len(inspect.signature(guardrails.inspect_output).parameters) >= 4:
            output_result = await guardrails.inspect_output(
                latest_prompt, answer, guardrail_level, input_result
            )
        else:
            output_result = await guardrails.inspect_output(
                latest_prompt, answer, guardrail_level
            )
    except GuardrailUnavailable as exc:
        GUARDRAIL_CHECK_DURATION_SECONDS.labels(
            guardrail_level, "output", output_guardrail_backend
        ).observe(perf_counter() - output_guardrail_started_at)
        GUARDRAIL_CHECKS.labels(guardrail_level, "output", output_guardrail_backend, "error").inc()
        output_span.record_exception(exc)
        output_span.end()
        logger.warning("Output guardrail unavailable; withholding the model response: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if output_result.stages:
        GUARDRAIL_CHECK_DURATION_SECONDS.labels(
            guardrail_level, "output", output_guardrail_backend
        ).observe(perf_counter() - output_guardrail_started_at)
        GUARDRAIL_CHECKS.labels(
            guardrail_level, "output", output_guardrail_backend, output_result.outcome
        ).inc()
    output_span.set_attribute(telemetry.ATTR_OUTCOME, output_result.outcome)
    output_span.set_attribute(telemetry.ATTR_LATENCY_MS, output_result.latency_ms)
    output_span.end()
    if output_result.outcome == "blocked":
        answer = "This response was withheld by the output safety check."

    verdict = build_verdict(guardrail_level, input_result, output_result)
    prompt_tokens = int(result["prompt_tokens"])
    completion_tokens = int(result["completion_tokens"])
    response = ChatResponse(
        answer=answer,
        model=decision.model,
        latency_ms=round((perf_counter() - start) * 1000, 2),
        usage=ChatUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        guardrail=verdict,
        sanitized_user_message=latest_prompt,
        cost=build_cost_info(backend.provider, decision.model, prompt_tokens, completion_tokens),
        trace_id=trace_id,
        fallback_used=decision.fallback_used,
        fallback_from=decision.fallback_from,
        attempts=len(decision.attempted_models),
    )
    return response, f"chatcmpl-{uuid4().hex}"


@app.get("/health", response_model=HealthResponse)
async def health(backend: BackendDependency, guardrails: GuardrailDependency) -> HealthResponse:
    connected = await backend.is_available()
    classifier_configured = await guardrails.classifier_is_configured()
    return HealthResponse(
        status="ok" if connected else "degraded",
        backend=backend.provider,
        model=backend.model,
        llm_connected=connected,
        guardrail_level=settings.guardrail_level,
        guardrail_classifier_backend=settings.llama_guard_backend,
        guardrail_classifier_configured=classifier_configured,
        adaptive_enabled=settings.adaptive_enabled,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/v1/models")
async def list_models(backend: BackendDependency) -> dict[str, object]:
    try:
        model_ids = await backend.list_models()
    except ModelServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "owned_by": backend.provider}
            for model_id in model_ids
        ],
    }


@app.get("/api/pricing")
async def pricing() -> dict[str, object]:
    try:
        return catalog_document(get_catalog())
    except PricingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/observability/config")
async def observability_config() -> dict[str, object]:
    return {
        "tracing_enabled": telemetry.is_enabled(),
        "content_capture_enabled": settings.otel_capture_content or settings.trace_capture_content,
        "phoenix_url": settings.phoenix_ui_url,
        "prometheus_url": settings.prometheus_url,
        "grafana_url": settings.grafana_url,
        "adaptive_enabled": settings.adaptive_enabled,
        "auth_enabled": bool(settings.api_auth_token),
        "trusted_hosts": list(settings.trusted_hosts),
        "max_body_bytes": settings.max_body_bytes,
        "generation_concurrency": settings.generation_concurrency,
        "alert_rules": [
            "/api/evaluations/summary",
            "prometheus: chat availability, guardrail p95, eval aborts",
        ],
    }


@app.get("/api/traces")
async def list_traces(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, object]:
    return {"traces": TRACE_STORE.list(limit=limit), "dropped_total": TRACE_STORE.dropped_total}


@app.get("/api/traces/{trace_id}")
async def get_trace(trace_id: str) -> dict[str, object]:
    record = TRACE_STORE.get(trace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Trace not found or retention expired.")
    return record


@app.get("/api/evaluations/summary")
async def evaluation_summary(manager: EvalManagerDependency) -> dict[str, object]:
    return {**manager.dataset_summary(), "runs": manager.store.list_runs(limit=10)}


@app.get("/api/evaluations/runs")
async def evaluation_runs(manager: EvalManagerDependency) -> dict[str, object]:
    return {"runs": manager.store.list_runs(limit=50)}


@app.post("/api/evaluations/runs", status_code=202)
async def start_evaluation(
    request: EvaluationStartRequest,
    backend: BackendDependency,
    guardrails: GuardrailDependency,
    manager: EvalManagerDependency,
) -> dict[str, object]:
    try:
        available_models = await backend.list_models()
    except ModelServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    generation_model = request.generation_model or backend.model
    if generation_model not in available_models:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{generation_model}' is not available from the configured provider.",
        )
    if "adaptive" in request.arms and not settings.adaptive_enabled:
        raise HTTPException(
            status_code=400,
            detail="The adaptive arm is disabled (ADAPTIVE_ENABLED=false).",
        )
    arms_needing_classifier = {"full", "input", "output", "adaptive"}
    if arms_needing_classifier.intersection(request.arms) and not (
        await guardrails.classifier_is_configured()
    ):
        raise HTTPException(
            status_code=400,
            detail="Configure the guardrail classifier before running classifier arms.",
        )
    async def call_chat(chat_request: ChatRequest, session_id: str) -> ChatResponse:
        # Evaluation pins the model: fallback is disabled for the whole run.
        response, _ = await complete_chat(
            chat_request,
            backend,
            guardrails,
            session_id,
            allow_fallback=False,
            trace_kind="eval_case",
        )
        return response

    try:
        return await manager.start_run(
            arms=request.arms,
            seed=request.seed,
            sample_limit=request.sample_limit,
            generation_model=generation_model,
            generation_backend=backend.provider,
            guardrail_backend=settings.llama_guard_backend,
            call_chat=call_chat,
            replicates=request.replicates,
            max_requests=request.max_requests,
        )
    except EvaluationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvaluationBudgetExceeded as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/evaluations/runs/{run_id}")
async def get_evaluation_run(
    run_id: str, manager: EvalManagerDependency
) -> dict[str, object]:
    run = manager.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return run


@app.get("/api/evaluations/runs/{run_id}/export.csv")
async def export_evaluation_run(run_id: str, manager: EvalManagerDependency) -> Response:
    csv_content = manager.store.export_csv(run_id)
    if csv_content is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="evaluation-{run_id}.csv"'},
    )


def _run_or_404(run_id: str, manager: EvaluationRunManager) -> dict[str, object]:
    run = manager.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return run


def _is_partial_run(run: dict[str, object]) -> bool:
    """Return True for budget-truncated runs, which are not comparable."""
    return bool(run.get("partial")) or run.get("status") == "aborted_budget"


@app.get("/api/evaluations/runs/{run_id}/report.html")
async def run_report_html(
    run_id: str,
    manager: EvalManagerDependency,
    include_content: Annotated[bool, Query()] = False,
) -> Response:
    run = _run_or_404(run_id, manager)
    if include_content:
        logger.warning("Content-inclusive report requested for run %s", run_id)
    html = render_run_report(run, options=ReportOptions(include_content=include_content))
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/api/evaluations/runs/{run_id}/report.json")
async def run_report_json(
    run_id: str,
    manager: EvalManagerDependency,
    include_content: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    run = _run_or_404(run_id, manager)
    if include_content:
        logger.warning("Content-inclusive report JSON requested for run %s", run_id)
    return render_report_json(run, options=ReportOptions(include_content=include_content))


@app.get("/api/reports/compare.html")
async def compare_report_html(
    manager: EvalManagerDependency,
    run_ids: Annotated[list[str] | None, Query()] = None,
    include_partial: Annotated[bool, Query()] = False,
) -> Response:
    runs = [_run_or_404(run_id, manager) for run_id in run_ids or []]
    html = render_comparison_report(
        runs,
        options=ReportOptions(include_partial=include_partial),
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/api/evaluations/compare")
async def compare_runs(
    manager: EvalManagerDependency,
    run_ids: Annotated[list[str] | None, Query()] = None,
    include_partial: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    runs = [_run_or_404(run_id, manager) for run_id in run_ids or []]
    partial = [run for run in runs if _is_partial_run(run)]
    headline = runs if include_partial else [run for run in runs if not _is_partial_run(run)]
    return {
        "runs": [
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "partial": run.get("partial", False),
                "generation_model": run.get("generation_model"),
                "summary": run.get("summary", {}),
            }
            for run in headline
        ],
        "excluded_partial_runs": [
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "partial": run.get("partial", False),
                "results_written": run.get("results_written"),
                "planned_requests": run.get("planned_requests"),
            }
            for run in partial
        ],
    }


@app.post("/api/compare")
async def compare_variants(
    request: CompareRequest,
    backend: BackendDependency,
    guardrails: GuardrailDependency,
) -> dict[str, object]:
    """Exploratory side-by-side calls; never written into evaluation runs."""
    outputs: list[dict[str, object]] = []
    for variant in request.variants:
        variant_request = ChatRequest(
            messages=request.messages,
            model=variant.model or request.model,
            guardrail_level=variant.guardrail_level or request.guardrail_level,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            seed=request.seed,
        )
        response, _ = await complete_chat(
            variant_request, backend, guardrails, trace_kind="compare"
        )
        outputs.append(
            {
                "model": response.model,
                "guardrail": response.guardrail.model_dump(),
                "answer": response.answer,
                "latency_ms": response.latency_ms,
                "cost": response.cost.model_dump() if response.cost else None,
                "trace_id": response.trace_id,
            }
        )
    return {"variants": outputs}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    backend: BackendDependency,
    guardrails: GuardrailDependency,
    session_id: Annotated[str | None, Header(alias="x-opencode-session")] = None,
) -> ChatResponse:
    response, _ = await complete_chat(request, backend, guardrails, session_id)
    return response


@app.post("/v1/chat/completions")
async def openai_chat_completions(
    request: ChatRequest,
    backend: BackendDependency,
    guardrails: GuardrailDependency,
    session_id: Annotated[str | None, Header(alias="x-opencode-session")] = None,
) -> dict[str, object]:
    response, completion_id = await complete_chat(request, backend, guardrails, session_id)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(datetime.now(UTC).timestamp()),
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.answer},
                "finish_reason": "content_filter" if response.guardrail.blocked else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
        "guardrail": response.guardrail.model_dump(),
        "sanitized_user_message": response.sanitized_user_message,
        "cost": response.cost.model_dump() if response.cost else None,
        "trace_id": response.trace_id,
        "fallback_used": response.fallback_used,
        "fallback_from": response.fallback_from,
    }
