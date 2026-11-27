"""Which invoice the voice agent dials: the workflow's queue first, the CSV only when
the workflow has never been synced."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from shared.models import CallOutcome, Customer, OutcomeType, RiskTier
from tests.conftest import make_invoice
from voice_agent.agent import NoCallDue, pick_call_target
from workflow import service

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 24, 11, 0, tzinfo=IST)


def invoice(invoice_id: str, days: int):
    return make_invoice(
        invoice_id=invoice_id,
        customer=Customer(customer_id=f"C-{invoice_id}", name=invoice_id),
        days_overdue=days,
    )


@pytest.fixture
async def db(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("CALL_INVOICE_ID", raising=False)
    import ab_dashboard.db as db_module
    import data.generate_dataset as dataset
    from shared.config import get_settings

    monkeypatch.setattr(
        dataset, "load_invoices", lambda: [invoice("CSV-LOW", 10), invoice("CSV-CRIT", 120)]
    )
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    await db_module.init_db()
    yield
    await db_module.dispose_engine()
    get_settings.cache_clear()


async def test_empty_workflow_falls_back_to_the_csv_queue(db) -> None:
    inv, tier = await pick_call_target(now=NOW)
    assert (inv.invoice_id, tier) == ("CSV-CRIT", RiskTier.CRITICAL)


async def test_workflow_queue_wins_once_synced(db) -> None:
    await service.sync_invoices([invoice("WF-HIGH", 60)], now=NOW)
    inv, tier = await pick_call_target(now=NOW)
    assert (inv.invoice_id, tier) == ("WF-HIGH", RiskTier.HIGH)


async def test_the_call_carries_the_customers_whole_account(db) -> None:
    same_customer = Customer(customer_id="C-1", name="Acme")
    await service.sync_invoices(
        [
            make_invoice(invoice_id="OLD", customer=same_customer, days_overdue=60),
            make_invoice(invoice_id="NEW", customer=same_customer, days_overdue=20),
        ],
        now=NOW,
    )
    inv, _ = await pick_call_target(now=NOW)
    assert inv.invoice_id == "OLD"
    assert [i.invoice_id for i in inv.other_open_invoices] == ["NEW"]
    assert "account_total_overdue: 100000.00 INR" in inv.context_block()


async def test_nothing_due_does_not_fall_back_to_the_csv(db) -> None:
    await service.sync_invoices([invoice("WF-HIGH", 60)], now=NOW)
    await service.record_call_outcome(
        "WF-HIGH", CallOutcome(outcome=OutcomeType.NO_ANSWER), now=NOW
    )
    # Just called: the contact rules say leave them alone, so no call at all.
    with pytest.raises(NoCallDue):
        await pick_call_target(now=NOW)


async def test_pinned_invoice_uses_the_workflow_view(db, monkeypatch) -> None:
    await service.sync_invoices([invoice("WF-MED", 30)], now=NOW)
    await service.record_call_outcome(
        "WF-MED",
        CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=NOW.date()),
        now=NOW,
    )
    monkeypatch.setenv("CALL_INVOICE_ID", "WF-MED")
    inv, _ = await pick_call_target(now=NOW)
    assert inv.promise_to_pay_date == NOW.date()


async def test_pinned_invoice_not_synced_comes_from_the_csv(db, monkeypatch) -> None:
    monkeypatch.setenv("CALL_INVOICE_ID", "CSV-LOW")
    inv, tier = await pick_call_target(now=NOW)
    assert (inv.invoice_id, tier) == ("CSV-LOW", RiskTier.LOW)
