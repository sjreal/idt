"""JSON logging, secret redaction, and prompt-omission helpers."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_REDACTED = "[REDACTED]"
OMITTED_PROMPT = "[omitted]"
JSON_HANDLER_NAME = "llm_app_json"

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bapi[_-]?key\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&]+)"),
    re.compile(
        r"(?i)\b[A-Z][A-Z0-9_]*(?:API_KEY|API_TOKEN|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*"
        r"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s&]+)"
    ),
    re.compile(r"\b[0-9A-Fa-f]{32,}\b"),
)

_STANDARD_FIELDS = frozenset(
    logging.LogRecord("", logging.NOTSET, "", 0, "", (), None).__dict__
) | {"message", "asctime"}

_LOG_PROMPTS_ENABLED = False


def redact(text: str) -> str:
    """Replace known secret shapes in ``text`` with a fixed placeholder."""
    result = text or ""
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def _scrub(value: Any) -> Any:
    """Recursively redact string values without changing non-string types."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


class RedactingFilter(logging.Filter):
    """Scrub log messages and arguments before handlers format or emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub(record.msg)
        record.args = _scrub(record.args)
        return True


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per record with redacted messages and extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS:
                payload[key] = _scrub(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = redact(self.formatStack(record.stack_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def _coerce_level(level: str) -> int:
    resolved = getattr(logging, str(level).upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(level: str = "INFO", log_prompts: bool = False) -> None:
    """Install a redacting JSON handler on the root logger.

    Under pytest, existing handlers are preserved so capture fixtures keep working;
    in a normal application run the root logger uses only the JSON handler.
    """
    global _LOG_PROMPTS_ENABLED
    handler = logging.StreamHandler()
    handler.set_name(JSON_HANDLER_NAME)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [item for item in root.handlers if item.get_name() != JSON_HANDLER_NAME]
    if "pytest" not in sys.modules:
        root.handlers = []
    root.addHandler(handler)
    root.setLevel(_coerce_level(level))
    _LOG_PROMPTS_ENABLED = bool(log_prompts)


def safe_prompt(prompt: str, log_prompts: bool = False) -> str:
    """Return prompt text only when prompt logging has been explicitly enabled."""
    return prompt if log_prompts or _LOG_PROMPTS_ENABLED else OMITTED_PROMPT
