"""HTTP request and response models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def require_user_message(self) -> "ChatRequest":
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("At least one user message is required.")
        return self


class ChatUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    answer: str
    model: str
    latency_ms: float
    usage: ChatUsage


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    backend: str
    model: str
    llm_connected: bool
