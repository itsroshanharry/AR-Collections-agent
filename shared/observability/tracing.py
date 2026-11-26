"""OpenTelemetry tracing, off by default and zero-cost until switched on.

Code everywhere calls `tracer.start_as_current_span(...)`. With only the OpenTelemetry
API installed (a core dependency), those calls are no-ops. `setup_tracing()` swaps in a
real SDK exporter when one is configured. Any combination may be set at once:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # Jaeger, Tempo, Honeycomb, ...
    LANGFUSE_PUBLIC_KEY=pk-lf-...                       # Langfuse, for LLM traces
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_HOST=https://cloud.langfuse.com            # or us./jp. region, or self-hosted
    OTEL_TRACES_CONSOLE=1                               # print spans, for local debugging

Langfuse ingests standard OTLP, so no Langfuse SDK is needed: the same spans go to it,
and its GenAI mapping turns `llm.complete` spans into generations with model, tokens and
cost. Prompt and completion text is only attached when explicitly enabled (see
`capture_message_content`), because call transcripts contain customer names and figures.

The SDK and exporter live in the optional `observability` extra, so the voice agent and
tests do not need them.
"""

from __future__ import annotations

import base64
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
    console = _truthy(os.getenv("OTEL_TRACES_CONSOLE"))
    langfuse = langfuse_exporter_config()
    if not endpoint and not console and not langfuse:
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
    if langfuse:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**langfuse)))
    if console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info(
        "Tracing enabled for %s (%s)",
        service_name,
        ", ".join(
            filter(
                None,
                [
                    endpoint and f"OTLP {endpoint}",
                    langfuse and f"Langfuse {langfuse['endpoint']}",
                    console and "console",
                ],
            )
        ),
    )
    return True


def is_enabled() -> bool:
    return _configured


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def langfuse_exporter_config() -> dict | None:
    """OTLP exporter arguments for Langfuse, or None if its keys are not set.

    Built here rather than through OTEL_EXPORTER_OTLP_HEADERS so the Basic-auth header
    needs no hand-encoding, and so Langfuse can run alongside a second OTLP backend.
    """
    public = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret = os.getenv("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        return None
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {
        "endpoint": f"{host}/api/public/otel/v1/traces",
        "headers": {"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"},
    }


def capture_message_content() -> bool:
    """Attach prompt and completion text to LLM spans?

    Off by default: transcripts carry customer names, amounts and GSTINs, and traces
    usually have wider access and longer retention than the database. The variable name
    is the OpenTelemetry GenAI instrumentation standard.
    """
    return _truthy(os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"))


def current_trace_ids() -> tuple[str | None, str | None]:
    """Hex trace and span id of the active span, for stamping onto log lines."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
