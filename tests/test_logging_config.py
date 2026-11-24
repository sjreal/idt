"""Tests for JSON logging, redaction, and prompt-omission helpers."""

import json
import logging

import pytest

import llm_app.logging_config as logging_config
from llm_app.logging_config import (
    JSONFormatter,
    RedactingFilter,
    configure_logging,
    redact,
    safe_prompt,
)


@pytest.fixture(autouse=True)
def restore_logging_state():
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers = handlers
    root.setLevel(level)
    logging_config._LOG_PROMPTS_ENABLED = False


@pytest.mark.parametrize(
    "secret",
    [
        "sk-not-a-real-key",
        "Bearer abc.def-ghi_123",
        "api_key=supersecretvalue",
        "api-key: another-secret",
        "OPENCODE_GO_API_KEY=env-secret-value",
        "CLOUDFLARE_API_TOKEN=cf-secret-value",
        "0123456789abcdef0123456789abcdef",
        "A" * 40,
    ],
)
def test_redact_removes_each_secret_pattern(secret: str) -> None:
    redacted = redact(f"before {secret} after")
    assert "[REDACTED]" in redacted
    assert secret not in redacted
    assert "before" in redacted
    assert "after" in redacted


def test_redact_is_deterministic_and_idempotent() -> None:
    text = "key sk-not-a-real-key and api_key=value"
    assert redact(text) == redact(text)
    assert redact(redact(text)) == redact(text)


def test_json_formatter_emits_single_line_redacted_json() -> None:
    record = logging.LogRecord(
        name="llm_app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=%s",
        args=("sk-not-a-real-key",),
        exc_info=None,
    )
    record.request_id = "req-123"

    encoded = JSONFormatter().format(record)

    assert "\n" not in encoded
    assert "sk-not-a-real-key" not in encoded
    payload = json.loads(encoded)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "llm_app.test"
    assert payload["message"] == "token=[REDACTED]"
    assert payload["request_id"] == "req-123"
    assert payload["timestamp"]
    assert "msg" not in payload
    assert "args" not in payload


def test_redacting_filter_scrubs_message_and_args() -> None:
    record = logging.LogRecord(
        name="llm_app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=2,
        msg="key=%s id=%s",
        args=("Bearer abc123", "safe-id"),
        exc_info=None,
    )

    assert RedactingFilter().filter(record) is True
    assert "Bearer abc123" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    assert "safe-id" in record.getMessage()


def test_redacting_filter_scrubs_mapping_args() -> None:
    record = logging.LogRecord(
        name="llm_app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=3,
        msg="%(api_key)s",
        args=({"api_key": "sk-not-a-real-key"},),
        exc_info=None,
    )

    RedactingFilter().filter(record)

    assert "sk-not-a-real-key" not in record.getMessage()
    assert record.getMessage() == "[REDACTED]"


def test_safe_prompt_omits_by_default_and_includes_when_enabled() -> None:
    assert safe_prompt("my prompt") == "[omitted]"
    assert safe_prompt("my prompt", log_prompts=False) == "[omitted]"
    assert safe_prompt("my prompt", log_prompts=True) == "my prompt"
    configure_logging(log_prompts=True)
    assert safe_prompt("my prompt") == "my prompt"
    configure_logging()
    assert safe_prompt("my prompt") == "[omitted]"


def test_configure_logging_installs_a_redacting_json_handler() -> None:
    configure_logging(level="DEBUG")

    root = logging.getLogger()
    handler = next(item for item in root.handlers if isinstance(item.formatter, JSONFormatter))
    assert any(isinstance(item, RedactingFilter) for item in handler.filters)
    assert root.level == logging.DEBUG
    configure_logging(level="INFO")
    json_handlers = [
        item for item in root.handlers if isinstance(item.formatter, JSONFormatter)
    ]
    assert len(json_handlers) == 1
