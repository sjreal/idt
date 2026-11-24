"""Environment-backed application settings."""

from __future__ import annotations

import os
from functools import lru_cache

VALID_GUARDRAIL_LEVELS = {
    "off",
    "prefilter",
    "scanners",
    "input",
    "output",
    "full",
    "adaptive",
}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int_env(name: str) -> int | None:
    """Return an integer env override, or ``None`` when the variable is unset.

    ``None`` lets a lower-precedence source (the committed experiment config)
    supply the value instead of an unrelated hard-coded default.
    """
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


@lru_cache
def get_settings() -> Settings:
    guardrail_level = os.getenv("GUARDRAIL_LEVEL", "off").lower()
    if guardrail_level not in VALID_GUARDRAIL_LEVELS:
        raise ValueError(
            "GUARDRAIL_LEVEL must be one of: " + ", ".join(sorted(VALID_GUARDRAIL_LEVELS))
        )
    return Settings(
        llm_backend=os.getenv("LLM_BACKEND", "ollama").lower(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
        opencode_go_base_url=os.getenv(
            "OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"
        ).rstrip("/"),
        opencode_go_model=os.getenv("OPENCODE_GO_MODEL", "deepseek-v4.1-flash"),
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
        evaluation_results_dir=os.getenv("EVALUATION_RESULTS_DIR", "results"),
        cors_origins=tuple(
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if origin.strip()
        ),
        # Telemetry
        otel_exporter_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip(),
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "llm-guardrail-lab"),
        otel_capture_content=_env_flag("OTEL_CAPTURE_CONTENT", False),
        trace_capture_content=_env_flag("TRACE_CAPTURE_CONTENT", False),
        trace_dir=os.getenv("TRACE_DIR", "traces"),
        trace_retention_days=int(os.getenv("TRACE_RETENTION_DAYS", "7")),
        trace_max_bytes=int(os.getenv("TRACE_MAX_BYTES", "50000000")),
        phoenix_ui_url=os.getenv("PHOENIX_UI_URL", "http://localhost:6006").rstrip("/"),
        prometheus_url=os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/"),
        grafana_url=os.getenv("GRAFANA_URL", "http://localhost:3001").rstrip("/"),
        # Guardrail depth
        prefilter_block_threshold=float(os.getenv("PREFILTER_BLOCK_THRESHOLD", "0.80")),
        prefilter_allow_threshold=float(os.getenv("PREFILTER_ALLOW_THRESHOLD", "0.20")),
        adaptive_enabled=_env_flag("ADAPTIVE_ENABLED", True),
        # Evaluation budget
        evaluation_default_max_requests=_optional_int_env("EVALUATION_DEFAULT_MAX_REQUESTS"),
        evaluation_hard_max_requests=int(os.getenv("EVALUATION_HARD_MAX_REQUESTS", "300")),
        # Routing
        opencode_go_fallback_models=tuple(
            model.strip()
            for model in os.getenv("OPENCODE_GO_FALLBACK_MODELS", "").split(",")
            if model.strip()
        ),
        circuit_failure_threshold=int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3")),
        circuit_cooldown_seconds=float(os.getenv("CIRCUIT_COOLDOWN_SECONDS", "60")),
        # Hardening
        api_auth_token=os.getenv("API_AUTH_TOKEN", ""),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")),
        max_body_bytes=int(os.getenv("MAX_BODY_BYTES", "1000000")),
        log_prompts=_env_flag("LOG_PROMPTS", False),
        generation_concurrency=int(os.getenv("GENERATION_CONCURRENCY", "2")),
        trusted_hosts=tuple(
            host.strip()
            for host in os.getenv("TRUSTED_HOSTS", "").split(",")
            if host.strip()
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
        evaluation_results_dir: str,
        cors_origins: tuple[str, ...],
        *,
        otel_exporter_endpoint: str = "",
        otel_service_name: str = "llm-guardrail-lab",
        otel_capture_content: bool = False,
        trace_capture_content: bool = False,
        trace_dir: str = "traces",
        trace_retention_days: int = 7,
        trace_max_bytes: int = 50_000_000,
        phoenix_ui_url: str = "http://localhost:6006",
        prometheus_url: str = "http://localhost:9090",
        grafana_url: str = "http://localhost:3001",
        prefilter_block_threshold: float = 0.80,
        prefilter_allow_threshold: float = 0.20,
        adaptive_enabled: bool = True,
        evaluation_default_max_requests: int | None = None,
        evaluation_hard_max_requests: int = 300,
        opencode_go_fallback_models: tuple[str, ...] = (),
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 60.0,
        api_auth_token: str = "",
        rate_limit_per_minute: int = 60,
        max_body_bytes: int = 1_000_000,
        log_prompts: bool = False,
        generation_concurrency: int = 2,
        trusted_hosts: tuple[str, ...] = (),
    ) -> None:
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model
        self.llm_backend = llm_backend
        self.opencode_go_base_url = opencode_go_base_url
        self.opencode_go_model = opencode_go_model
        self.opencode_go_api_key = opencode_go_api_key
        if guardrail_level not in VALID_GUARDRAIL_LEVELS:
            raise ValueError(f"Unsupported guardrail level: {guardrail_level}")
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
        self.evaluation_results_dir = evaluation_results_dir
        self.cors_origins = cors_origins

        # Telemetry
        self.otel_exporter_endpoint = otel_exporter_endpoint
        self.otel_service_name = otel_service_name
        self.otel_capture_content = otel_capture_content
        self.trace_capture_content = trace_capture_content
        self.trace_dir = trace_dir
        self.trace_retention_days = trace_retention_days
        self.trace_max_bytes = trace_max_bytes
        self.phoenix_ui_url = phoenix_ui_url
        self.prometheus_url = prometheus_url
        self.grafana_url = grafana_url

        # Guardrail depth
        if not 0 <= prefilter_allow_threshold <= prefilter_block_threshold <= 1:
            raise ValueError(
                "Prefilter thresholds must satisfy 0 <= allow <= block <= 1"
            )
        self.prefilter_block_threshold = prefilter_block_threshold
        self.prefilter_allow_threshold = prefilter_allow_threshold
        self.adaptive_enabled = adaptive_enabled

        # Evaluation budget. The env default is optional so the committed
        # experiment config can supply it; the hard ceiling always applies.
        if evaluation_hard_max_requests < 1:
            raise ValueError("Evaluation request limits must be positive")
        if evaluation_default_max_requests is not None:
            if evaluation_default_max_requests < 1:
                raise ValueError("Evaluation request limits must be positive")
            if evaluation_default_max_requests > evaluation_hard_max_requests:
                raise ValueError(
                    "EVALUATION_DEFAULT_MAX_REQUESTS cannot exceed the hard ceiling"
                )
        self.evaluation_default_max_requests = evaluation_default_max_requests
        self.evaluation_hard_max_requests = evaluation_hard_max_requests

        # Routing
        self.opencode_go_fallback_models = opencode_go_fallback_models
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds

        # Hardening
        self.api_auth_token = api_auth_token
        self.rate_limit_per_minute = rate_limit_per_minute
        self.max_body_bytes = max_body_bytes
        self.log_prompts = log_prompts
        self.generation_concurrency = max(1, generation_concurrency)
        self.trusted_hosts = trusted_hosts
