"""Minimal asynchronous Ollama chat client."""

from collections.abc import Sequence
from time import monotonic
from typing import Any

import httpx


class ModelServiceError(Exception):
    """Raised when the configured model service cannot complete a request."""


class OllamaBackend:
    provider = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
        excluded_models: tuple[str, ...] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.excluded_models = set(excluded_models)
        self._models_cache: list[str] = []
        self._models_cached_at = 0.0

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        selected_model = model or self.model
        options: dict[str, int | float] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if seed is not None:
            options["seed"] = seed
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": list(messages),
            "stream": False,
        }
        if options:
            payload["options"] = options

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ModelServiceError(
                    f"Ollama model '{selected_model}' is not available. Pull it with "
                    f"`ollama pull {selected_model}`."
                ) from exc
            raise ModelServiceError(
                "Ollama returned an error while generating a response."
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise ModelServiceError(
                "Cannot reach Ollama. Start the Ollama service and try again."
            ) from exc
        except (ValueError, KeyError) as exc:
            raise ModelServiceError("Ollama returned an invalid response.") from exc

        answer = data.get("message", {}).get("content")
        if not isinstance(answer, str):
            raise ModelServiceError("Ollama returned an invalid response.")

        return {
            "content": answer,
            "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
            "completion_tokens": data.get("eval_count", 0) or 0,
        }

    async def list_models(self) -> list[str]:
        now = monotonic()
        if self._models_cache and now - self._models_cached_at < 15:
            return self._models_cache.copy()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=min(self.timeout_seconds, 5),
                transport=self.transport,
            ) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                data = response.json()
            available_models = sorted(
                {
                    entry.get("name")
                    for entry in data.get("models", [])
                    if isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and entry.get("name") not in self.excluded_models
                }
            )
            self._models_cache = available_models
            self._models_cached_at = now
            return available_models.copy()
        except (httpx.HTTPError, AttributeError, TypeError, ValueError) as exc:
            raise ModelServiceError("Cannot load the available models from Ollama.") from exc

    async def is_available(self) -> bool:
        try:
            return self.model in await self.list_models()
        except ModelServiceError:
            return False
