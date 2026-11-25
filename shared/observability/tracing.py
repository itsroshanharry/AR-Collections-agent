"""OpenTelemetry tracing, off by default and zero-cost until switched on.

Code everywhere calls `tracer.start_as_current_span(...)`. With only the OpenTelemetry
API installed (a core dependency), those calls are no-ops. `setup_tracing()` swaps in a
real SDK exporter when one is configured:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # Jaeger, Tempo, Honeycomb, ...
    OTEL_TRACES_CONSOLE=1                               # print spans, for local debugging

The SDK and exporter live in the optional `observability` extra, so the voice agent and
tests do not need them.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("ar-collections")

_configured = False


def setup_tracing(service_name: str) -> bool:
    """Install an exporting tracer provider if one is configured. Idempotent.

    Returns whether spans are now being exported.
    """
    global _configured
    if _configured:
        return True

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    console = os.getenv("OTEL_TRACES_CONSOLE", "").lower() in ("1", "true", "yes")
    if not endpoint and not console:
        return False

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        logger.warning(
            "Tracing requested but opentelemetry-sdk is missing. "
            "Install it with: uv sync --extra observability"
        )
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # Reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS itself, per the OTel spec.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    if console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info(
        "Tracing enabled for %s (%s)",
        service_name,
        ", ".join(filter(None, [endpoint and f"OTLP {endpoint}", console and "console"])),
    )
    return True


def current_trace_ids() -> tuple[str | None, str | None]:
    """Hex trace and span id of the active span, for stamping onto log lines."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
