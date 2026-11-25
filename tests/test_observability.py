"""Structured logs, trace correlation, request ids and tracing setup."""

from __future__ import annotations

import json
import logging

import pytest

from shared.observability import tracing
from shared.observability.logging import JsonFormatter, configure_logging, request_id_var


def record(msg: str = "hello", **extra) -> logging.LogRecord:
    rec = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": msg})
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


class TestJsonFormatter:
    def test_one_json_object_per_line(self) -> None:
        line = JsonFormatter().format(record("paid %s", args=None))
        entry = json.loads(line)
        assert entry["message"] == "paid %s"
        assert entry["level"] == "INFO"
        assert "\n" not in line

    def test_extra_fields_are_kept(self) -> None:
        entry = json.loads(JsonFormatter().format(record(invoice_id="INV-1", attempt=2)))
        assert (entry["invoice_id"], entry["attempt"]) == ("INV-1", 2)

    def test_request_id_is_stamped(self) -> None:
        token = request_id_var.set("req-42")
        try:
            entry = json.loads(JsonFormatter().format(record()))
        finally:
            request_id_var.reset(token)
        assert entry["request_id"] == "req-42"

    def test_trace_and_span_ids_join_logs_to_traces(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider

        tracer = TracerProvider().get_tracer("test")
        with tracer.start_as_current_span("work") as span:
            entry = json.loads(JsonFormatter().format(record()))
            ctx = span.get_span_context()
        assert entry["trace_id"] == f"{ctx.trace_id:032x}"
        assert entry["span_id"] == f"{ctx.span_id:016x}"

    def test_no_trace_ids_outside_a_span(self) -> None:
        entry = json.loads(JsonFormatter().format(record()))
        assert "trace_id" not in entry

    def test_no_placeholder_request_id_outside_a_request(self) -> None:
        entry = json.loads(JsonFormatter().format(record(request_id="-", color_message="x")))
        assert "request_id" not in entry
        assert "color_message" not in entry

    def test_exceptions_are_serialised(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = record(exc_info=sys.exc_info())
        assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exception"]


def test_configure_logging_replaces_handlers() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("json", "warning")
        configure_logging("json", "warning")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])


class TestTracingSetup:
    def test_off_without_configuration(self, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_TRACES_CONSOLE", raising=False)
        monkeypatch.setattr(tracing, "_configured", False)
        assert tracing.setup_tracing("svc") is False

    def test_console_exporter_enables_tracing(self, monkeypatch) -> None:
        from opentelemetry import trace

        monkeypatch.setenv("OTEL_TRACES_CONSOLE", "1")
        monkeypatch.setattr(tracing, "_configured", False)
        installed = []
        monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
        assert tracing.setup_tracing("svc") is True
        assert installed, "a tracer provider should have been installed"


def test_request_id_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rid.db'}")
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    from fastapi.testclient import TestClient

    from ab_dashboard.main import app

    with TestClient(app) as client:
        echoed = client.get("/health", headers={"X-Request-ID": "abc-123"})
        generated = client.get("/health")

    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()

    assert echoed.headers["x-request-id"] == "abc-123"
    assert len(generated.headers["x-request-id"]) == 16


@pytest.fixture(autouse=True)
def _restore_request_id():
    yield
    assert request_id_var.get() == "", "request id leaked out of its request"
