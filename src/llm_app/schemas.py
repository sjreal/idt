"""HTTP request and response models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The plan matrix has seven arms. ``GuardrailLevel`` is kept as a backwards-compatible
# alias so existing imports and stored results keep working.
GuardrailArm = Literal[
    "off", "prefilter", "scanners", "input", "output", "full", "adaptive"
]
GuardrailLevel = GuardrailArm

VALID_GUARDRAIL_ARMS: tuple[str, ...] = (
    "off",
    "prefilter",
    "scanners",
    "input",
    "output",
    "full",
    "adaptive",
)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    seed: int | None = None
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


class CostInfo(BaseModel):
    priced: bool
    notional_usd: float | None = None
    marginal_usd: float | None = None
    currency: str = "USD"
    billing_mode: str = "unknown"
    pricing_sha256: str | None = None
    unpriced_reason: str | None = None


class GuardrailFinding(BaseModel):
    scanner: str
    outcome: Literal["passed", "redacted", "blocked"]
    risk_score: float | None = None
    category: str | None = None


class GuardrailStage(BaseModel):
    """A per-stage record so reports can attribute blocks and sum classifier calls."""

    stage: str
    backend: str
    outcome: Literal["passed", "redacted", "blocked", "skipped", "review"]
    blocked_by: str | None = None
    category: str | None = None
    latency_ms: float = 0.0
    findings: list[GuardrailFinding] = Field(default_factory=list)


class GuardrailVerdict(BaseModel):
    level: GuardrailLevel
    outcome: Literal["passed", "redacted", "blocked"]
    blocked: bool
    blocked_by: str | None = None
    category: str | None = None
    latency_ms: float
    findings: list[GuardrailFinding] = Field(default_factory=list)
    stages: list[GuardrailStage] = Field(default_factory=list)
    classifier_calls: int = 0
    prefilter: dict[str, object] | None = None


class ChatResponse(BaseModel):
    answer: str
    model: str
    latency_ms: float
    usage: ChatUsage
    guardrail: GuardrailVerdict
    sanitized_user_message: str | None = None
    cost: CostInfo | None = None
    trace_id: str | None = None
    fallback_used: bool = False
    fallback_from: str | None = None
    attempts: int = 1


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    backend: str
    model: str
    llm_connected: bool
    guardrail_level: GuardrailLevel
    guardrail_classifier_backend: Literal["cloudflare", "ollama"]
    guardrail_classifier_configured: bool
    adaptive_enabled: bool = True


class CompareVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    guardrail_level: GuardrailLevel | None = None


class CompareRequest(BaseModel):
    """Exploratory side-by-side request; never written to evaluation runs."""

    model_config = ConfigDict(extra="ignore")

    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    model: str | None = None
    guardrail_level: GuardrailLevel | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    seed: int | None = None
    variants: list[CompareVariant] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def require_user_message(self) -> "CompareRequest":
        if self.messages[-1].role != "user":
            raise ValueError("The latest message must be from the user.")
        return self


class EvaluationStartRequest(BaseModel):
    arms: list[GuardrailLevel] = Field(
        default_factory=lambda: ["off", "scanners", "full"], min_length=1, max_length=7
    )
    seed: int = 42
    sample_limit: int | None = Field(default=None, ge=1, le=100)
    generation_model: str | None = None
    replicates: int = Field(default=1, ge=1, le=10)
    max_requests: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_unique_arms(self) -> "EvaluationStartRequest":
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("Guardrail arms must be unique.")
        return self
