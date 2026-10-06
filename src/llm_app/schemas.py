"""HTTP request and response models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GuardrailLevel = Literal["off", "scanners", "full"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False
    guardrail_level: GuardrailLevel | None = None

    @model_validator(mode="after")
    def require_user_message(self) -> "ChatRequest":
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("At least one user message is required.")
        if self.messages[-1].role != "user":
            raise ValueError("The latest message must be from the user.")
        return self


class ChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class GuardrailFinding(BaseModel):
    scanner: str
    outcome: Literal["passed", "redacted", "blocked"]
    risk_score: float | None = None
    category: str | None = None


class GuardrailVerdict(BaseModel):
    level: GuardrailLevel
    outcome: Literal["passed", "redacted", "blocked"]
    blocked: bool
    blocked_by: str | None = None
    category: str | None = None
    latency_ms: float
    findings: list[GuardrailFinding] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    model: str
    latency_ms: float
    usage: ChatUsage
    guardrail: GuardrailVerdict
    sanitized_user_message: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    backend: str
    model: str
    llm_connected: bool
    guardrail_level: GuardrailLevel
    guardrail_classifier_backend: Literal["cloudflare", "ollama"]
    guardrail_classifier_configured: bool
