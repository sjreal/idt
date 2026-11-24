"""Optional OpenTelemetry GenAI tracing with a dependency-free no-op fallback.

Tracing is only activated when an OTLP/HTTP endpoint is configured *and* the
OpenTelemetry packages are importable. Setup failures are logged and never
propagated to the request path. Prompt/answer content is not exported unless
capture is explicitly enabled.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "llm-guardrail-lab"
REDACTED = "[redacted]"

# GenAI semantic-convention keys where available, project keys otherwise.
ATTR_ARM = "llm.arm"
ATTR_MODEL = "gen_ai.request.model"
ATTR_STAGE = "llm.guardrail.stage"
ATTR_OUTCOME = "llm.outcome"
ATTR_BLOCKED_BY = "llm.blocked_by"
ATTR_LATENCY_MS = "llm.latency_ms"
ATTR_USAGE_INPUT = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_ATTEMPTS = "llm.attempts"
ATTR_TRACE_ID = "llm.trace_id"

# The OTLP/HTTP signal path appended to a collector base URL.
_TRACES_PATH = "/v1/traces"

_SENSITIVE_KEY_PARTS = ("prompt", "answer", "content", "output", "input")
_METRIC_KEY_PARTS = ("token", "latency", "duration", "count", "size")


class NoOpSpan:
    """Minimal span that satisfies the context-manager API used by the app."""

    def set_attribute(self, key: str, value: object) -> None:
        """Ignore an attribute update."""

    def set_attributes(self, attributes: dict[str, object]) -> None:
        """Ignore a batch of attribute updates."""

    def add_event(self, name: str, attributes: dict[str, object] | None = None) -> None:
        """Ignore an event."""

    def record_exception(self, exception: BaseException, **kwargs: Any) -> None:
        """Ignore an exception record."""

    def set_status(self, status: Any) -> None:
        """Ignore a status update."""

    def get_span_context(self) -> None:
        """Return no span context."""
        return None

    @property
    def is_recording(self) -> bool:
        """Report that nothing is being recorded."""
        return False

    def end(self) -> None:
        """Finish the span (no work to do)."""

    def __enter__(self) -> NoOpSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class NoOpTracer:
    """Tracer used when OTLP export is disabled or unavailable."""

    def start_as_current_span(
        self,
        name: str,
        attributes: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> NoOpSpan:
        """Return an inert span that can be used as a context manager."""
        return NoOpSpan()

    def start_span(
        self,
        name: str,
        attributes: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> NoOpSpan:
        """Return an inert span that can be used as a context manager."""
        return NoOpSpan()


_NOOP_TRACER = NoOpTracer()

_trace_api: Any = None
_provider: Any = None
_enabled = False
_capture_content = False
_endpoint: str | None = None
_service_name = DEFAULT_SERVICE_NAME


def _load_otel() -> tuple[Any, Any, Any, Any, Any, Any] | None:
    """Import OpenTelemetry lazily so the app works without the dependency."""
    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.info("OpenTelemetry is not installed; tracing stays disabled (%s)", exc)
        return None
    return (
        trace_api,
        OTLPSpanExporter,
        Resource,
        TracerProvider,
        BatchSpanProcessor,
    )


def _shutdown_provider() -> None:
    """Release any exporter/processor created by :func:`configure`."""
    global _enabled, _provider, _trace_api
    provider, _provider = _provider, None
    _trace_api = None
    _enabled = False
    if provider is not None:
        try:
            provider.shutdown()
        except Exception as exc:  # pragma: no cover - defensive against SDK versions
            logger.debug("OpenTelemetry provider shutdown failed: %s", exc)


def _resolve_traces_endpoint(endpoint: str) -> str:
    """Return the full OTLP/HTTP traces URL for ``endpoint``.

    The OTLP environment convention treats ``OTEL_EXPORTER_OTLP_ENDPOINT`` as a
    base URL and appends the signal path, while an explicit traces endpoint is
    used verbatim. Accept both so the value configured for this app produces
    the same URL as the exporters that resolve it from the environment.
    """
    trimmed = endpoint.strip().rstrip("/")
    if trimmed.endswith(_TRACES_PATH):
        return trimmed
    return f"{trimmed}{_TRACES_PATH}"


def configure(service_name: str, endpoint: str | None, capture_content: bool = False) -> None:
    """Configure OTLP/HTTP tracing, or disable it when export is unavailable.

    The endpoint may be the collector base URL or a full ``/v1/traces`` URL; it
    is resolved to the traces URL the exporter should post to. This function
    never raises on exporter/setup failures so it is safe to call during startup.
    """
    global _capture_content, _enabled, _endpoint, _provider, _service_name, _trace_api
    _shutdown_provider()
    _service_name = service_name or DEFAULT_SERVICE_NAME
    _capture_content = bool(capture_content)
    _endpoint = endpoint.strip() if endpoint else None

    if not _endpoint:
        logger.info("OTLP endpoint not configured; tracing is disabled")
        return

    otel = _load_otel()
    if otel is None:
        return
    (
        trace_api,
        exporter_cls,
        resource_cls,
        provider_cls,
        processor_cls,
    ) = otel
    try:
        exporter = exporter_cls(endpoint=_resolve_traces_endpoint(_endpoint))
        provider = provider_cls(resource=resource_cls.create({"service.name": _service_name}))
        provider.add_span_processor(processor_cls(exporter))
        # The provider is intentionally NOT installed as the global one. The
        # OpenTelemetry API refuses to override a tracer provider once it is
        # set, so reconfiguring (tests, reloads) would silently keep the first,
        # possibly shut-down, provider. Tracers are taken from this provider
        # directly; only the span *context* is global, which is all the
        # parenting and trace-id lookups need.
        _trace_api = trace_api
        _provider = provider
        _enabled = True
        logger.info("OTLP tracing enabled for %s", _endpoint)
    except Exception as exc:
        logger.warning("Could not initialize OTLP tracing; disabling it: %s", exc)
        _shutdown_provider()


def reset() -> None:
    """Clear all module state (intended for tests and service shutdown)."""
    global _capture_content, _endpoint, _service_name
    _shutdown_provider()
    _capture_content = False
    _endpoint = None
    _service_name = DEFAULT_SERVICE_NAME


def is_enabled() -> bool:
    """Return whether a real OTLP tracer provider is active."""
    return _enabled


def content_enabled() -> bool:
    """Return whether prompt/answer content may be exported."""
    return _capture_content


def new_trace_id() -> str:
    """Return a new 32-character lowercase hexadecimal trace id."""
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    """Return the active OTel trace id, or None when tracing is inactive."""
    if not _enabled or _trace_api is None:
        return None
    try:
        span_context = _trace_api.get_current_span().get_span_context()
        trace_id = span_context.trace_id
        return f"{trace_id:032x}" if trace_id else None
    except Exception as exc:  # pragma: no cover - defensive against SDK versions
        logger.debug("Could not read the active span context: %s", exc)
        return None


def span_trace_id(span: Any) -> str | None:
    """Return the trace id carried by ``span``, or ``None`` when it has none.

    Reading the id from the span itself (rather than from the global tracer
    API) keeps it correct even when the span was not made the current span.
    """
    try:
        span_context = span.get_span_context()
    except Exception as exc:  # pragma: no cover - defensive against SDK versions
        logger.debug("Could not read the span context: %s", exc)
        return None
    if span_context is None:
        return None
    trace_id = getattr(span_context, "trace_id", 0)
    if not trace_id:
        return None
    return f"{trace_id:032x}"


def attach_span(span: Any) -> Any:
    """Make ``span`` the current span and return a detach token.

    Child spans created afterwards are then nested under it, so the exported
    trace has the shape the GenAI conventions describe. Returns ``None`` for the
    no-op span used when tracing is disabled, so the OpenTelemetry packages are
    not imported in that case.
    """
    if isinstance(span, NoOpSpan):
        return None
    try:
        from opentelemetry import context as otel_context

        trace_api = _trace_api
        if trace_api is None:
            from opentelemetry import trace as trace_api

        return otel_context.attach(trace_api.set_span_in_context(span))
    except Exception as exc:  # pragma: no cover - defensive against SDK versions
        logger.debug("Could not attach the span context: %s", exc)
        return None


def detach_span(token: Any) -> None:
    """Detach a span context previously returned by :func:`attach_span`."""
    if token is None:
        return
    try:
        from opentelemetry import context as otel_context

        otel_context.detach(token)
    except Exception as exc:  # pragma: no cover - defensive against SDK versions
        logger.debug("Could not detach the span context: %s", exc)


def get_tracer(name: str = DEFAULT_SERVICE_NAME) -> NoOpTracer | Any:
    """Return a real OTel tracer when enabled, otherwise a no-op tracer."""
    if _enabled and _provider is not None:
        try:
            return _provider.get_tracer(name)
        except Exception as exc:  # pragma: no cover - defensive against SDK versions
            logger.warning("Could not obtain tracer %s; using no-op tracer: %s", name, exc)
    return _NOOP_TRACER


def safe_attributes(**kwargs: object) -> dict[str, object]:
    """Build span attributes, dropping None and redacting content by default.

    Numeric metric values (token counts, latency, sizes) are preserved even
    when their key mentions an input/output concept; text-like values under
    sensitive keys are replaced with ``"[redacted]"``.
    """
    attributes: dict[str, object] = {}
    redacted = 0
    for key, value in kwargs.items():
        if value is None:
            continue
        lower_key = key.lower()
        sensitive = any(part in lower_key for part in _SENSITIVE_KEY_PARTS)
        numeric_metric = isinstance(value, (int, float)) and any(
            part in lower_key for part in _METRIC_KEY_PARTS
        )
        if sensitive and not numeric_metric and not _capture_content:
            attributes[key] = REDACTED
            redacted += 1
        else:
            attributes[key] = value
    if redacted:
        logger.debug("Redacted %d sensitive span attribute(s)", redacted)
    return attributes
