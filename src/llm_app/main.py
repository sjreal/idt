"""FastAPI application exposing app-native and OpenAI-compatible chat APIs."""

import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from llm_app.backends.base import ChatBackend
from llm_app.backends.ollama import ModelServiceError, OllamaBackend
from llm_app.backends.opencode_go import OpenCodeGoBackend
from llm_app.guardrails import (
    GuardrailPipeline,
    GuardrailUnavailable,
    build_verdict,
)
from llm_app.metrics import (
    GENERATION_REQUEST_DURATION_SECONDS,
    GENERATION_REQUESTS,
    GENERATION_TOKENS,
    GUARDRAIL_CHECKS,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS,
)
from llm_app.schemas import (
    ChatRequest,
    ChatResponse,
    ChatUsage,
    EvaluationStartRequest,
    HealthResponse,
)
from llm_app.settings import get_settings
from security_eval.runner import EvaluationAlreadyRunning, EvaluationRunManager

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="LLM Guardrail Lab API",
    description="Local LLM chat API and OpenAI-compatible endpoint for evaluation tools.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "x-opencode-session"],
)


@app.middleware("http")
async def collect_http_metrics(request, call_next):
    started_at = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        endpoint = getattr(route, "path", "unmatched")
        if endpoint != "/metrics":
            method = request.method
            HTTP_REQUESTS.labels(method, endpoint, str(status_code)).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method, endpoint).observe(
                perf_counter() - started_at
            )


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


def require_supported_model(request: ChatRequest, backend: ChatBackend) -> None:
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported yet. Set stream to false.",
        )


def guardrail_metric_backend(level: str) -> str:
    if level == "full":
        return settings.llama_guard_backend
    if level == "scanners":
        return "llm-guard"
    return "none"


async def complete_chat(
    request: ChatRequest,
    backend: ChatBackend,
    guardrails: GuardrailPipeline,
    session_id: str | None = None,
) -> tuple[ChatResponse, str]:
    require_supported_model(request, backend)
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
    start = perf_counter()
    try:
        input_result = await guardrails.inspect_input(
            [message.model_dump() for message in request.messages], guardrail_level
        )
    except GuardrailUnavailable as exc:
        GUARDRAIL_CHECKS.labels(
            guardrail_level, "input", guardrail_metric_backend(guardrail_level), "error"
        ).inc()
        logger.warning("Input guardrail unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    GUARDRAIL_CHECKS.labels(
        guardrail_level,
        "input",
        guardrail_metric_backend(guardrail_level),
        input_result.outcome,
    ).inc()

    if input_result.outcome == "blocked":
        verdict = build_verdict(guardrail_level, input_result)
        response = ChatResponse(
            answer="This message was blocked by the input safety checks.",
            model=selected_model,
            latency_ms=round((perf_counter() - start) * 1000, 2),
            usage=ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            guardrail=verdict,
        )
        return response, f"chatcmpl-{uuid4().hex}"

    latest_prompt = input_result.messages[-1]["content"]
    generation_started_at = perf_counter()
    generation_outcome = "error"
    try:
        result = await backend.chat(
            input_result.messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            seed=request.seed,
            model=selected_model,
            session_id=session_id,
        )
        generation_outcome = "success"
    except ModelServiceError as exc:
        logger.warning("LLM backend request failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        GENERATION_REQUESTS.labels(
            backend.provider, selected_model, generation_outcome
        ).inc()
        GENERATION_REQUEST_DURATION_SECONDS.labels(
            backend.provider, selected_model
        ).observe(perf_counter() - generation_started_at)

    GENERATION_TOKENS.labels(backend.provider, selected_model, "prompt").inc(
        int(result["prompt_tokens"])
    )
    GENERATION_TOKENS.labels(backend.provider, selected_model, "completion").inc(
        int(result["completion_tokens"])
    )

    answer = result["content"]
    output_result = None
    if guardrail_level == "full":
        try:
            output_result = await guardrails.inspect_output(latest_prompt, answer, guardrail_level)
        except GuardrailUnavailable as exc:
            GUARDRAIL_CHECKS.labels(
                guardrail_level, "output", guardrail_metric_backend(guardrail_level), "error"
            ).inc()
            logger.warning("Output guardrail unavailable; withholding the model response: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        GUARDRAIL_CHECKS.labels(
            guardrail_level,
            "output",
            guardrail_metric_backend(guardrail_level),
            output_result.outcome,
        ).inc()
        if output_result.outcome == "blocked":
            answer = "This response was withheld by the output safety check."

    verdict = build_verdict(guardrail_level, input_result, output_result)
    prompt_tokens = int(result["prompt_tokens"])
    completion_tokens = int(result["completion_tokens"])
    response = ChatResponse(
        answer=answer,
        model=selected_model,
        latency_ms=round((perf_counter() - start) * 1000, 2),
        usage=ChatUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        guardrail=verdict,
        sanitized_user_message=latest_prompt,
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
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(
        content=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


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


@app.get("/api/evaluations/summary")
async def evaluation_summary() -> dict[str, object]:
    manager = get_evaluation_manager()
    return {**manager.dataset_summary(), "runs": manager.store.list_runs(limit=10)}


@app.get("/api/evaluations/runs")
async def evaluation_runs() -> dict[str, object]:
    return {"runs": get_evaluation_manager().store.list_runs(limit=50)}


@app.post("/api/evaluations/runs", status_code=202)
async def start_evaluation(
    request: EvaluationStartRequest,
    backend: BackendDependency,
    guardrails: GuardrailDependency,
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
    if "full" in request.arms and not await guardrails.classifier_is_configured():
        raise HTTPException(
            status_code=400,
            detail="Configure the guardrail classifier before running the full arm.",
        )
    manager = get_evaluation_manager()

    async def call_chat(chat_request: ChatRequest, session_id: str) -> ChatResponse:
        response, _ = await complete_chat(chat_request, backend, guardrails, session_id)
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
        )
    except EvaluationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/evaluations/runs/{run_id}")
async def get_evaluation_run(run_id: str) -> dict[str, object]:
    run = get_evaluation_manager().store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return run


@app.get("/api/evaluations/runs/{run_id}/export.csv")
async def export_evaluation_run(run_id: str) -> Response:
    csv_content = get_evaluation_manager().store.export_csv(run_id)
    if csv_content is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found.")
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="evaluation-{run_id}.csv"'},
    )


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
    }
