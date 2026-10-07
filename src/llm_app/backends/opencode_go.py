"""OpenAI Chat Completions adapter for OpenCode Go models."""

import asyncio
import logging
from collections.abc import Sequence
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from llm_app.backends.ollama import ModelServiceError
from llm_app.metrics import GENERATION_RETRIES

logger = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class _InvalidModelResponse(Exception):
    """Raised when a successful provider response has no usable answer."""


class OpenCodeGoBackend:
    """Call an OpenCode Go model using its OpenAI-compatible endpoint."""

    provider = "opencode-go"

    # These Go catalog models use the Chat Completions endpoint. Models served
    # through Responses or Anthropic Messages need a different adapter.
    chat_completion_models = frozenset(
        {
            "glm-5.3-flash",
            "glm-5.3",
            "glm-5.2",
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.6",
            "longcat-2.0",
            "longcat-2.5-preview-free",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "hy4-preview",
            "hy3",
            "space-bunny",
        }
    )

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "guardrail-lab/0.1.0",
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.user_agent = user_agent
        self.max_retries = max(0, min(max_retries, 2))
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._models_cache: list[str] = []
        self._models_cached_at = 0.0

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": self.user_agent,
            "x-opencode-session": session_id or f"guardrail-lab-{uuid4().hex}",
        }

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ModelServiceError(
                "OpenCode Go is selected but OPENCODE_GO_API_KEY is not configured."
            )

        selected_model = model or self.model
        if selected_model not in self.chat_completion_models:
            raise ModelServiceError(
                f"OpenCode Go model '{selected_model}' is not in the supported "
                "Chat Completions model list."
            )
        payload: dict[str, Any] = {"model": selected_model, "messages": list(messages)}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if seed is not None:
            payload["seed"] = seed

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=f"{self.base_url}/",
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = await client.post(
                        "chat/completions",
                        json=payload,
                        headers=self._headers(session_id),
                    )

                status = response.status_code
                if status in RETRYABLE_STATUS_CODES:
                    if await self._wait_before_retry(
                        attempt, selected_model, status=status
                    ):
                        continue
                    raise ModelServiceError(
                        f"OpenCode Go returned HTTP {status} after {attempt + 1} attempts."
                    )
                if status in (401, 403):
                    raise ModelServiceError(
                        "OpenCode Go rejected the API key. Check OPENCODE_GO_API_KEY."
                    )
                if not response.is_success:
                    raise ModelServiceError(
                        f"OpenCode Go returned HTTP {status} for this request."
                    )

                try:
                    data = response.json()
                    return self._parse_response(data)
                except (ValueError, _InvalidModelResponse) as exc:
                    if await self._wait_before_retry(
                        attempt, selected_model, invalid_response=True
                    ):
                        continue
                    raise ModelServiceError(
                        f"{exc} (after {attempt + 1} attempts)."
                    ) from exc
            except httpx.RequestError as exc:
                if await self._wait_before_retry(
                    attempt, selected_model, request_error=True
                ):
                    continue
                raise ModelServiceError(
                    f"Cannot reach the OpenCode Go API after {attempt + 1} attempts."
                ) from exc

        raise AssertionError("Retry loop exited without a response or an error.")

    async def _wait_before_retry(
        self,
        attempt: int,
        model: str,
        *,
        status: int | None = None,
        invalid_response: bool = False,
        request_error: bool = False,
    ) -> bool:
        if attempt >= self.max_retries:
            return False
        delay = self.retry_backoff_seconds * (2**attempt)
        reason = (
            f"HTTP {status}"
            if status is not None
            else "invalid response"
            if invalid_response
            else "request timeout/network error"
            if request_error
            else "provider error"
        )
        metric_reason = (
            f"http_{status}"
            if status is not None
            else "invalid_response"
            if invalid_response
            else "request_error"
            if request_error
            else "provider_error"
        )
        GENERATION_RETRIES.labels(self.provider, model, metric_reason).inc()
        logger.warning(
            "OpenCode Go %s; retry %s/%s in %.1f seconds",
            reason,
            attempt + 1,
            self.max_retries,
            delay,
        )
        await asyncio.sleep(delay)
        return True

    @staticmethod
    def _parse_response(data: Any) -> dict[str, Any]:
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            usage = data.get("usage") or {}
            if not isinstance(content, str) or not content.strip():
                raise _InvalidModelResponse(
                    "OpenCode Go did not return a final text answer. Increase max_tokens "
                    "or select a Chat Completions model with a non-reasoning response."
                )
            return {
                "content": content,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            }
        except _InvalidModelResponse:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise _InvalidModelResponse("OpenCode Go returned an invalid response.") from exc

    async def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            return self.model in await self.list_models()
        except ModelServiceError:
            return False

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return [self.model] if self.model in self.chat_completion_models else []
        now = monotonic()
        if self._models_cache and now - self._models_cached_at < 60:
            return self._models_cache.copy()

        try:
            async with httpx.AsyncClient(
                base_url=f"{self.base_url}/",
                timeout=min(self.timeout_seconds, 10),
                transport=self.transport,
            ) as client:
                response = await client.get(
                    "models", headers=self._headers("guardrail-lab-model-list")
                )
                response.raise_for_status()
                data = response.json()
            model_ids = {
                item.get("id")
                for item in data.get("data", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            self._models_cache = sorted(model_ids & self.chat_completion_models)
            self._models_cached_at = now
            return self._models_cache.copy()
        except (httpx.HTTPError, AttributeError, TypeError, ValueError) as exc:
            raise ModelServiceError(
                "Cannot load available Chat Completions models from OpenCode Go."
            ) from exc
