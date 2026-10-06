"""Environment-backed application settings."""

import os
from functools import lru_cache


@lru_cache
def get_settings() -> "Settings":
    guardrail_level = os.getenv("GUARDRAIL_LEVEL", "off").lower()
    if guardrail_level not in {"off", "scanners", "full"}:
        raise ValueError("GUARDRAIL_LEVEL must be one of: off, scanners, full")
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
        guardrail_level=guardrail_level,
        llama_guard_backend=os.getenv("LLAMA_GUARD_BACKEND", "cloudflare").lower(),
        llama_guard_base_url=os.getenv("LLAMA_GUARD_BASE_URL", "http://ollama:11434").rstrip("/"),
        llama_guard_model=os.getenv("LLAMA_GUARD_MODEL", "llama-guard3:1b"),
        llama_guard_timeout_seconds=float(os.getenv("LLAMA_GUARD_TIMEOUT_SECONDS", "90")),
        cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
        cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", ""),
        cloudflare_llama_guard_model=os.getenv(
            "CLOUDFLARE_LLAMA_GUARD_MODEL", "@cf/meta/llama-guard-3-8b"
        ),
        cloudflare_base_url=os.getenv(
            "CLOUDFLARE_API_BASE_URL", "https://api.cloudflare.com/client/v4"
        ).rstrip("/"),
        prompt_injection_threshold=float(os.getenv("PROMPT_INJECTION_THRESHOLD", "0.92")),
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
        guardrail_level: str,
        llama_guard_backend: str,
        llama_guard_base_url: str,
        llama_guard_model: str,
        llama_guard_timeout_seconds: float,
        cloudflare_account_id: str,
        cloudflare_api_token: str,
        cloudflare_llama_guard_model: str,
        cloudflare_base_url: str,
        prompt_injection_threshold: float,
        request_timeout_seconds: float,
        cors_origins: tuple[str, ...],
    ) -> None:
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model
        self.llm_backend = llm_backend
        self.opencode_go_base_url = opencode_go_base_url
        self.opencode_go_model = opencode_go_model
        self.opencode_go_api_key = opencode_go_api_key
        self.guardrail_level = guardrail_level
        if llama_guard_backend not in {"cloudflare", "ollama"}:
            raise ValueError("LLAMA_GUARD_BACKEND must be one of: cloudflare, ollama")
        self.llama_guard_backend = llama_guard_backend
        self.llama_guard_base_url = llama_guard_base_url
        self.llama_guard_model = llama_guard_model
        self.llama_guard_timeout_seconds = llama_guard_timeout_seconds
        self.cloudflare_account_id = cloudflare_account_id
        self.cloudflare_api_token = cloudflare_api_token
        self.cloudflare_llama_guard_model = cloudflare_llama_guard_model
        self.cloudflare_base_url = cloudflare_base_url
        self.prompt_injection_threshold = prompt_injection_threshold
        self.request_timeout_seconds = request_timeout_seconds
        self.cors_origins = cors_origins
