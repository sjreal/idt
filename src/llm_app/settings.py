"""Environment-backed application settings."""

import os
from functools import lru_cache


@lru_cache
def get_settings() -> "Settings":
    return Settings(
        llm_backend=os.getenv("LLM_BACKEND", "ollama").lower(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
        opencode_go_base_url=os.getenv(
            "OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"
        ).rstrip("/"),
        opencode_go_model=os.getenv(
            "OPENCODE_GO_MODEL", "longcat-2.5-preview-free"
        ),
        opencode_go_api_key=os.getenv("OPENCODE_GO_API_KEY", ""),
        request_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        cors_origins=tuple(
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if origin.strip()
        ),
    )


class Settings:
    def __init__(
        self,
        llm_backend: str,
        ollama_base_url: str,
        ollama_model: str,
        opencode_go_base_url: str,
        opencode_go_model: str,
        opencode_go_api_key: str,
        request_timeout_seconds: float,
        cors_origins: tuple[str, ...],
    ) -> None:
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model
        self.llm_backend = llm_backend
        self.opencode_go_base_url = opencode_go_base_url
        self.opencode_go_model = opencode_go_model
        self.opencode_go_api_key = opencode_go_api_key
        self.request_timeout_seconds = request_timeout_seconds
        self.cors_origins = cors_origins
