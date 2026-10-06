"""Minimal asynchronous Ollama chat client."""

from collections.abc import Sequence
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        options = {"temperature": temperature} if temperature is not None else None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
        }
        if options is not None:
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
                    f"Ollama model '{self.model}' is not available. Pull it with "
                    f"`ollama pull {self.model}`."
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

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=min(self.timeout_seconds, 5),
                transport=self.transport,
            ) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                data = response.json()
            available_models = {
                entry.get("name")
                for entry in data.get("models", [])
                if isinstance(entry, dict)
            }
            return self.model in available_models
        except httpx.HTTPError:
            return False
        except (AttributeError, ValueError):
            return False
