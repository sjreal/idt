"""Tests for the optional OpenTelemetry tracing wrapper."""

from __future__ import annotations

import builtins
import socket
import uuid
from importlib import import_module
from typing import Any

import pytest

from llm_app import telemetry


@pytest.fixture(autouse=True)
def _clean_telemetry() -> None:
    telemetry.reset()
    yield
    telemetry.reset()


def test_configure_without_endpoint_disables_tracing_and_redacts() -> None:
    telemetry.configure("test-service", None)

    assert telemetry.is_enabled() is False
    assert telemetry.content_enabled() is False
    assert isinstance(telemetry.get_tracer("test"), telemetry.NoOpTracer)
    assert telemetry.current_trace_id() is None

    attributes = telemetry.safe_attributes(
        prompt="secret prompt",
        answer="secret answer",
        content="secret content",
        output="secret output",
        input="secret input",
        model="llama3.2:1b",
        empty=None,
    )
    for key in ("prompt", "answer", "content", "output", "input"):
        assert attributes[key] == "[redacted]"
    assert attributes["model"] == "llama3.2:1b"
    assert "empty" not in attributes


def test_noop_tracer_and_span_are_context_managers() -> None:
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("request", attributes={"a": 1}) as span:
        assert isinstance(span, telemetry.NoOpSpan)
        span.set_attribute("b", 2)
        span.set_attributes({"c": 3})
        span.add_event("event", {"d": 4})
        span.record_exception(ValueError("ignored"))
        span.set_status("OK")
        assert span.is_recording is False
        assert span.get_span_context() is None

    manual = tracer.start_span("manual")
    assert manual.__enter__() is manual
    manual.end()
    assert manual.__exit__(None, None, None) is False
    assert telemetry.current_trace_id() is None


def test_new_trace_id_is_32_lowercase_hex_and_unique() -> None:
    first = telemetry.new_trace_id()
    second = telemetry.new_trace_id()

    assert first != second
    for trace_id in (first, second):
        assert len(trace_id) == 32
        assert trace_id == trace_id.lower()
        assert uuid.UUID(trace_id).hex == trace_id


def test_disabled_tracing_does_not_import_otel_or_open_network_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise AssertionError("OpenTelemetry must not be imported when disabled")
        return real_import(name, *args, **kwargs)

    def fail_connection(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no network calls are allowed when tracing is disabled")

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", fail_import)
    monkeypatch.setattr(socket.socket, "connect", fail_connection)
    monkeypatch.setattr(socket, "create_connection", fail_connection)

    telemetry.configure("test-service", "")
    with telemetry.get_tracer().start_as_current_span("request", attributes={"a": 1}):
        pass
    assert telemetry.is_enabled() is False


def test_configure_with_endpoint_but_without_otel_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def import_without_otel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("simulated missing OpenTelemetry")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_otel)

    telemetry.configure("test-service", "http://localhost:4318/v1/traces")

    assert telemetry.is_enabled() is False
    assert isinstance(telemetry.get_tracer(), telemetry.NoOpTracer)
    assert telemetry.current_trace_id() is None


def test_content_capture_enabled_keeps_text_but_still_drops_none() -> None:
    telemetry.configure("test-service", None, capture_content=True)

    assert telemetry.content_enabled() is True
    assert telemetry.safe_attributes(prompt="secret", blank=None) == {"prompt": "secret"}


def test_numeric_usage_metrics_are_not_redacted() -> None:
    telemetry.configure("test-service", None)

    assert telemetry.safe_attributes(
        **{
            telemetry.ATTR_USAGE_INPUT: 12,
            telemetry.ATTR_USAGE_OUTPUT: 34,
            telemetry.ATTR_LATENCY_MS: 56.7,
            telemetry.ATTR_ATTEMPTS: 2,
        }
    ) == {
        telemetry.ATTR_USAGE_INPUT: 12,
        telemetry.ATTR_USAGE_OUTPUT: 34,
        telemetry.ATTR_LATENCY_MS: 56.7,
        telemetry.ATTR_ATTEMPTS: 2,
    }


def test_attribute_constants_use_genai_conventions() -> None:
    module = import_module("llm_app.telemetry")

    assert module.ATTR_MODEL == "gen_ai.request.model"
    assert module.ATTR_USAGE_INPUT == "gen_ai.usage.input_tokens"
    assert module.ATTR_USAGE_OUTPUT == "gen_ai.usage.output_tokens"
    for name in (
        "ATTR_ARM",
        "ATTR_STAGE",
        "ATTR_OUTCOME",
        "ATTR_BLOCKED_BY",
        "ATTR_LATENCY_MS",
        "ATTR_ATTEMPTS",
    ):
        assert isinstance(getattr(module, name), str)


def test_reset_clears_capture_and_enabled_state(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry.configure("test-service", None, capture_content=True)
    monkeypatch.setattr(telemetry, "_enabled", True)

    telemetry.reset()

    assert telemetry.is_enabled() is False
    assert telemetry.content_enabled() is False


def test_span_trace_id_matches_the_exported_trace_and_children_nest() -> None:
    """The trace id must come from the span, and child spans must share it."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    root = tracer.start_span("chat.request")
    trace_id = telemetry.span_trace_id(root)
    assert trace_id is not None
    assert len(trace_id) == 32

    token = telemetry.attach_span(root)
    try:
        child = tracer.start_span("gen_ai.chat")
        assert telemetry.span_trace_id(child) == trace_id
    finally:
        telemetry.detach_span(token)
    child.end()
    root.end()

    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert {"chat.request", "gen_ai.chat"} <= names
    for span in spans:
        assert f"{span.context.trace_id:032x}" == trace_id
    child_span = next(span for span in spans if span.name == "gen_ai.chat")
    root_span = next(span for span in spans if span.name == "chat.request")
    assert child_span.parent is not None
    assert child_span.parent.span_id == root_span.context.span_id


def test_span_trace_id_is_none_for_the_noop_span() -> None:
    assert telemetry.span_trace_id(telemetry.NoOpSpan()) is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://phoenix:6006", "http://phoenix:6006/v1/traces"),
        ("http://phoenix:6006/", "http://phoenix:6006/v1/traces"),
        ("http://phoenix:6006/v1/traces", "http://phoenix:6006/v1/traces"),
        ("http://phoenix:6006/v1/traces/", "http://phoenix:6006/v1/traces"),
        ("  http://collector:4318/v1/traces  ", "http://collector:4318/v1/traces"),
    ],
)
def test_traces_endpoint_resolution_accepts_base_and_full_urls(
    configured: str, expected: str
) -> None:
    """A base URL and a full traces URL must resolve to the same target."""
    assert telemetry._resolve_traces_endpoint(configured) == expected


def test_configure_uses_the_resolved_traces_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exporter must post to /v1/traces, not a doubled path."""
    captured: dict[str, str] = {}

    class FakeExporter:
        def __init__(self, endpoint: str) -> None:
            captured["endpoint"] = endpoint

    class FakeProcessor:
        def __init__(self, exporter: object) -> None:
            self.exporter = exporter

    class FakeProvider:
        def __init__(self, resource: object = None) -> None:
            pass

        def add_span_processor(self, processor: object) -> None:
            pass

        def get_tracer(self, name: str) -> object:
            return object()

        def shutdown(self) -> None:
            pass

    class FakeResource:
        @staticmethod
        def create(attributes: dict[str, str]) -> object:
            return object()

    fake_otel = (object(), FakeExporter, FakeResource, FakeProvider, FakeProcessor)
    monkeypatch.setattr(telemetry, "_load_otel", lambda: fake_otel)

    telemetry.configure("svc", "http://phoenix:6006")

    assert captured["endpoint"] == "http://phoenix:6006/v1/traces"
    telemetry.reset()
