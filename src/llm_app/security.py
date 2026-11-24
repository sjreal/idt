"""Self-contained request security helpers for the LLM Guardrail Lab API.

These helpers deliberately do not import application settings, so middleware,
dependencies, and tests can use them independently. Callers pass the configured
token explicitly.
"""

import hashlib
import hmac
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException

AUTH_ERROR_DETAIL = "Invalid or missing API token"
BODY_TOO_LARGE_DETAIL = "Request body too large."
UPSTREAM_ERROR_DETAIL = "Upstream service is unavailable."
MAX_BODY_BYTES = 1_000_000

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    ),
}

_KEY_COMPONENT_LIMIT = 64
_HASH_PREFIX_LENGTH = 32


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two values in constant time, tolerating ``None`` and Unicode text."""
    if a is None or b is None:
        return a is b
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _token_candidates(token: str | None, authorization_header: str | None) -> list[str]:
    """Collect the raw and ``Bearer <token>`` forms supplied by a caller."""
    candidates: list[str] = []
    for value in (token, authorization_header):
        if not value or not value.strip():
            continue
        stripped = value.strip()
        candidates.append(stripped)
        parts = stripped.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            candidates.append(parts[1].strip())
    return candidates


def check_auth(
    token: str | None,
    authorization_header: str | None,
    configured_token: str = "",
) -> None:
    """Reject unauthenticated callers when an API token has been configured.

    An empty ``configured_token`` disables authentication (local-development default).
    Both a raw token and the ``Bearer <token>`` header form are accepted. The configured
    token is never included in the error detail.
    """
    if not configured_token:
        return
    candidates = _token_candidates(token, authorization_header)
    if any(constant_time_equals(candidate, configured_token) for candidate in candidates):
        return
    raise HTTPException(status_code=401, detail=AUTH_ERROR_DETAIL)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Thread-safe per-key token bucket with a linear 60-second refill."""

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        capacity = per_minute if burst is None else burst
        if capacity <= 0:
            raise ValueError("burst must be positive")
        self.per_minute = per_minute
        self.burst = capacity
        self._refill_rate = per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Consume one token for ``key``; return ``(allowed, retry_after_seconds)``."""
        current = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated_at=current)
                self._buckets[key] = bucket
            elapsed = max(0.0, current - bucket.updated_at)
            bucket.updated_at = max(bucket.updated_at, current)
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self._refill_rate)
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True, 0
            retry_after = math.ceil((1 - bucket.tokens) / self._refill_rate)
            return False, max(1, retry_after)


def rate_limit_key(client_host: str | None, session_id: str | None) -> str:
    """Build a bounded, deterministic key from caller identifiers only.

    Long values are hashed with SHA-256 and truncated, so prompts or other sensitive
    text cannot leak through the rate-limit key or grow it without bound.
    """
    components: list[str] = []
    for value in (client_host, session_id):
        normalised = (value or "").strip()
        if len(normalised) > _KEY_COMPONENT_LIMIT:
            digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
            components.append(digest[:_HASH_PREFIX_LENGTH])
        else:
            components.append(normalised or "-")
    return "|".join(components)


def host_rate_limit_key(client_host: str | None) -> str:
    """Build the per-host rate-limit key.

    The session id is deliberately excluded: the caller controls it, so a key
    that includes it can be reset by the caller on every request. This key
    bounds the total request rate coming from one host.
    """
    normalised = (client_host or "").strip()
    if len(normalised) > _KEY_COMPONENT_LIMIT:
        digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
        normalised = digest[:_HASH_PREFIX_LENGTH]
    return f"host|{normalised or '-'}"


def enforce_body_limit(content_length: int | None, max_bytes: int = MAX_BODY_BYTES) -> None:
    """Reject a declared body length above ``max_bytes``; ``None`` means unknown."""
    if content_length is None:
        return
    if content_length < 0 or content_length > max_bytes:
        raise HTTPException(status_code=413, detail=BODY_TOO_LARGE_DETAIL)


def parse_content_length(raw: str | None) -> int | None:
    """Parse a ``Content-Length`` header into an int, or ``None`` when unusable.

    A malformed header is treated as unknown rather than trusted, so the streamed
    body limit still applies.
    """
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def limited_receive(receive, max_bytes: int = MAX_BODY_BYTES):
    """Wrap an ASGI receive channel and reject bodies larger than ``max_bytes``.

    The declared ``Content-Length`` header is not trusted: a chunked request can
    omit it, so the real number of bytes is counted as the body streams in.
    """
    received = 0

    async def _receive():
        nonlocal received
        message = await receive()
        if message.get("type") == "http.request":
            received += len(message.get("body") or b"")
            if received > max_bytes:
                raise HTTPException(status_code=413, detail=BODY_TOO_LARGE_DETAIL)
        return message

    return _receive


class BodySizeLimitMiddleware:
    """ASGI middleware that caps the request body size.

    This is a plain ASGI middleware rather than a ``BaseHTTPMiddleware``
    decorator on purpose: Starlette's base middleware reads the body through the
    receive channel it created itself, so a wrapper passed down from a
    ``@app.middleware("http")`` handler would never be consulted. Wrapping
    ``receive`` here is the only place the byte counter is guaranteed to see
    every chunk the application reads.

    ``max_bytes`` may be an int or a zero-argument callable, so the effective
    cap can be read from settings at request time instead of being frozen when
    the middleware is built.
    """

    def __init__(self, app, max_bytes: int | Callable[[], int] = MAX_BODY_BYTES) -> None:
        self.app = app
        self._max_bytes = max_bytes

    def _limit(self) -> int:
        if callable(self._max_bytes):
            return max(1, int(self._max_bytes()))
        return max(1, int(self._max_bytes))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.app(scope, limited_receive(receive, self._limit()), send)
