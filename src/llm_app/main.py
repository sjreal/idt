"""FastAPI application exposing app-native and OpenAI-compatible chat APIs."""

import logging
from datetime import UTC, datetime
from functools import lru_cache
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from llm_app.backends.base import ChatBackend
from llm_app.backends.ollama import ModelServiceError, OllamaBackend
from llm_app.backends.opencode_go import OpenCodeGoBackend
from llm_app.guardrails import (
    GuardrailPipeline,
    GuardrailUnavailable,
    build_verdict,
)
from llm_app.schemas import ChatRequest, ChatResponse, ChatUsage, HealthResponse
from llm_app.settings import get_settings

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


@lru_cache
def get_backend() -> ChatBackend:
    if settings.llm_backend == "ollama":
        return OllamaBackend(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.request_timeout_seconds,
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


def require_supported_model(request: ChatRequest, backend: ChatBackend) -> None:
    if request.model and request.model != backend.model:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{request.model}' is not configured on this server.",
        )
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported yet. Set stream to false.",
        )


async def complete_chat(
    request: ChatRequest,
    backend: ChatBackend,
    guardrails: GuardrailPipeline,
    session_id: str | None = None,
) -> tuple[ChatResponse, str]:
    require_supported_model(request, backend)
    guardrail_level = request.guardrail_level or settings.guardrail_level
    start = perf_counter()
    try:
        input_result = await guardrails.inspect_input(
            [message.model_dump() for message in request.messages], guardrail_level
        )
    except GuardrailUnavailable as exc:
        logger.warning("Input guardrail unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if input_result.outcome == "blocked":
        verdict = build_verdict(guardrail_level, input_result)
        response = ChatResponse(
            answer="This message was blocked by the input safety checks.",
            model=backend.model,
            latency_ms=round((perf_counter() - start) * 1000, 2),
            usage=ChatUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            guardrail=verdict,
        )
        return response, f"chatcmpl-{uuid4().hex}"

    latest_prompt = input_result.messages[-1]["content"]
    try:
        result = await backend.chat(
            input_result.messages,
            temperature=request.temperature,
            session_id=session_id,
        )
    except ModelServiceError as exc:
        logger.warning("LLM backend request failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    answer = result["content"]
    output_result = None
    if guardrail_level == "full":
        try:
            output_result = await guardrails.inspect_output(latest_prompt, answer, guardrail_level)
        except GuardrailUnavailable as exc:
            logger.warning("Output guardrail unavailable; withholding the model response: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if output_result.outcome == "blocked":
            answer = "This response was withheld by the output safety check."

    verdict = build_verdict(guardrail_level, input_result, output_result)
    prompt_tokens = int(result["prompt_tokens"])
    completion_tokens = int(result["completion_tokens"])
    response = ChatResponse(
        answer=answer,
        model=backend.model,
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


@app.get("/v1/models")
async def list_models(backend: BackendDependency) -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"id": backend.model, "object": "model", "owned_by": "local"}],
    }


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
