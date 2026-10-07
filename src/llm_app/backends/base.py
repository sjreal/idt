"""Shared backend protocol for chat providers."""

from collections.abc import Sequence
from typing import Any, Protocol


class ChatBackend(Protocol):
    provider: str
    model: str

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def list_models(self) -> list[str]: ...

    async def is_available(self) -> bool: ...
