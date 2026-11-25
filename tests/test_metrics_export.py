"""Prometheus metrics: recorded where the work happens, and exported on /metrics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from shared.models import CallOutcome, Customer, OutcomeType
from shared.observability.metrics import REGISTRY
from tests.conftest import make_invoice
from workflow import service

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 24, 11, 0, tzinfo=IST)


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
async def db(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    await db_module.init_db()
    yield
    await db_module.dispose_engine()
    get_settings.cache_clear()


def inv(invoice_id: str, days: int, **kw):
    return make_invoice(
        invoice_id=invoice_id,
        customer=Customer(customer_id=f"C-{invoice_id}", name=invoice_id),
        days_overdue=days,
        **kw,
    )


async def test_workflow_counts_contacts_transitions_and_tasks(db) -> None:
    emails = sample("dunning_contacts_total", channel="email")
    calls = sample("dunning_contacts_total", channel="call")
    disputes = sample("dunning_tasks_opened_total", task_type="review_dispute")

    await service.sync_invoices([inv("M1", 30), inv("H1", 60)], now=NOW)
    await service.tick(now=NOW)
    await service.record_call_outcome(
        "H1",
        CallOutcome(outcome=OutcomeType.DISPUTE_RAISED, dispute_reason="rate"),
        now=NOW,
    )

    assert sample("dunning_contacts_total", channel="email") == emails + 1
    assert sample("dunning_contacts_total", channel="call") == calls + 1
    assert sample("dunning_tasks_opened_total", task_type="review_dispute") == disputes + 1
    assert sample("dunning_tick_last_success_timestamp_seconds") > 0


async def test_gauges_reflect_current_state(db) -> None:
    await service.sync_invoices(
        [inv("D1", 30, dispute_flag=True), inv("H1", 60, amount=Decimal("1000"), currency="USD")],
        now=NOW,
    )
    await service.refresh_metrics(now=NOW)

    assert sample("dunning_invoices", state="disputed", tier="medium") == 1
    assert sample("dunning_open_tasks", assignee="disputes", task_type="review_dispute") == 1
    # USD is INR-normalised, so balances across currencies are comparable.
    assert sample("dunning_outstanding_inr", tier="high") == 88_000


async def test_gauges_drop_states_that_have_emptied(db) -> None:
    await service.sync_invoices([inv("D1", 30, dispute_flag=True)], now=NOW)
    await service.refresh_metrics(now=NOW)
    await service.record_payment("D1", Decimal("50000"), now=NOW)
    await service.refresh_metrics(now=NOW)

    assert sample("dunning_invoices", state="disputed", tier="medium") == 0.0
    assert sample("dunning_open_tasks", assignee="disputes", task_type="review_dispute") == 0.0


def test_metrics_endpoint_and_http_route_labels(database_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    from fastapi.testclient import TestClient

    from ab_dashboard.main import app

    with TestClient(app) as client:
        client.get("/tasks/abc123")  # PATCH-only route: 405, but still a known template
        client.get("/dunning/invoices/NOPE")
        body = client.get("/metrics").text

    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()

    assert "dunning_tick_last_success_timestamp_seconds" in body
    # Labelled by template, not by the id in the URL.
    assert 'route="/dunning/invoices/{invoice_id}"' in body
    assert "NOPE" not in body
