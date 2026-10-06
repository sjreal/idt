import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_app.backends.ollama import ModelServiceError, OllamaBackend
from llm_app.backends.opencode_go import OpenCodeGoBackend
from llm_app.main import app, get_backend


class FakeBackend:
    provider = "ollama"
    model = "test-model"

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        assert messages[-1]["role"] == "user"
        assert temperature is None or 0 <= temperature <= 2
        return {"content": "Hello from the fake model.", "prompt_tokens": 5, "completion_tokens": 5}

    async def is_available(self) -> bool:
        return True


def get_test_client(backend: FakeBackend | None = None) -> TestClient:
    app.dependency_overrides[get_backend] = lambda: backend or FakeBackend()
    return TestClient(app)


def test_health_reports_backend_status() -> None:
    with get_test_client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "backend": "ollama",
        "model": "test-model",
        "llm_connected": True,
    }
    app.dependency_overrides.clear()


def test_native_chat_returns_answer_and_usage() -> None:
    with get_test_client() as client:
        response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Hello from the fake model."
    assert body["model"] == "test-model"
    assert body["latency_ms"] >= 0
    assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
    app.dependency_overrides.clear()


def test_openai_compatible_endpoint_returns_completion_shape() -> None:
    with get_test_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Hello from the fake model.",
    }
    assert body["usage"]["total_tokens"] == 10
    app.dependency_overrides.clear()


def test_openai_models_lists_configured_model() -> None:
    with get_test_client() as client:
        response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "test-model"
    app.dependency_overrides.clear()


def test_request_without_user_message_is_rejected() -> None:
    with get_test_client() as client:
        response = client.post(
            "/api/chat", json={"messages": [{"role": "system", "content": "You are helpful."}]}
        )

    assert response.status_code == 422
    app.dependency_overrides.clear()


def test_backend_error_is_returned_as_bad_gateway() -> None:
    class BrokenBackend(FakeBackend):
        async def chat(
            self,
            messages: Sequence[dict[str, str]],
            temperature: float | None = None,
            session_id: str | None = None,
        ) -> dict[str, Any]:
            raise ModelServiceError("Ollama is unavailable.")

    with get_test_client(BrokenBackend()) as client:
        response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})

    assert response.status_code == 502
    assert response.json()["detail"] == "Ollama is unavailable."
    app.dependency_overrides.clear()


def test_ollama_backend_sends_chat_and_parses_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["model"] == "fixture-model"
        assert payload["stream"] is False
        return httpx.Response(
            200,
            json={
                "message": {"content": "Fixture reply"},
                "prompt_eval_count": 7,
                "eval_count": 4,
            },
        )

    backend = OllamaBackend(
        "http://ollama.test",
        "fixture-model",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(backend.chat([{"role": "user", "content": "Hello"}]))

    assert result == {"content": "Fixture reply", "prompt_tokens": 7, "completion_tokens": 4}


def test_ollama_health_requires_configured_model_to_be_pulled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "fixture-model"}]})

    backend = OllamaBackend(
        "http://ollama.test",
        "fixture-model",
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(backend.is_available()) is True


def test_opencode_go_uses_compatible_endpoint_and_session_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/zen/go/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.headers["user-agent"] == "guardrail-lab/0.1.0"
        assert request.headers["x-opencode-session"] == "stable-session-id"
        payload = json.loads(request.content)
        assert payload["model"] == "kimi-k2.7-code"
        assert payload["messages"] == [{"role": "user", "content": "Hello"}]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Go reply"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            },
        )

    backend = OpenCodeGoBackend(
        "https://opencode.ai/zen/go/v1",
        "kimi-k2.7-code",
        "test-secret",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        backend.chat(
            [{"role": "user", "content": "Hello"}],
            session_id="stable-session-id",
        )
    )

    assert result == {"content": "Go reply", "prompt_tokens": 8, "completion_tokens": 3}


def test_opencode_go_requires_api_key() -> None:
    backend = OpenCodeGoBackend("https://opencode.ai/zen/go/v1", "kimi-k2.7-code", "")

    with pytest.raises(ModelServiceError, match="OPENCODE_GO_API_KEY"):
        asyncio.run(backend.chat([{"role": "user", "content": "Hello"}]))
