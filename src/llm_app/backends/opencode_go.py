"""OpenAI Chat Completions adapter for OpenCode Go models."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import httpx

from llm_app.backends.ollama import ModelServiceError


class OpenCodeGoBackend:
    """Call an OpenCode Go model using its OpenAI-compatible endpoint."""

    provider = "opencode-go"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "guardrail-lab/0.1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.user_agent = user_agent

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
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ModelServiceError(
                "OpenCode Go is selected but OPENCODE_GO_API_KEY is not configured."
            )

        payload: dict[str, Any] = {"model": self.model, "messages": list(messages)}
        if temperature is not None:
            payload["temperature"] = temperature

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
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ModelServiceError(
                    "OpenCode Go rejected the API key. Check OPENCODE_GO_API_KEY."
                ) from exc
            raise ModelServiceError("OpenCode Go returned an error for this request.") from exc
        except httpx.RequestError as exc:
            raise ModelServiceError("Cannot reach the OpenCode Go API.") from exc
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ModelServiceError("OpenCode Go returned an invalid response.") from exc

        try:
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            if not isinstance(content, str):
                raise TypeError("Expected text message content")
            return {
                "content": content,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            }
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ModelServiceError("OpenCode Go returned an invalid response.") from exc

    async def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(
                base_url=f"{self.base_url}/",
                timeout=min(self.timeout_seconds, 10),
                transport=self.transport,
            ) as client:
                response = await client.get("models", headers=self._headers("guardrail-lab-health"))
                response.raise_for_status()
                data = response.json()
            models = data.get("data", [])
            return any(
                isinstance(model, dict) and model.get("id") == self.model for model in models
            )
        except (httpx.HTTPError, AttributeError, TypeError, ValueError):
            return False
