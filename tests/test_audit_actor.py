"""The actor is recorded on every audit event, task resolution and bank decision."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ab_dashboard.db import InvoiceEventRow, TaskRow
from tests.test_workflow_service import NOW, medium, open_tasks
from workflow import audit, cash, service
from workflow.cash_application import BankLine


async def events(db, invoice_id: str) -> list[InvoiceEventRow]:
    async with db.get_sessionmaker()() as session:
        rows = await session.execute(
            select(InvoiceEventRow)
            .where(InvoiceEventRow.invoice_id == invoice_id)
            .order_by(InvoiceEventRow.id)
        )
        return list(rows.scalars())


def test_actor_defaults_to_system_and_is_scoped() -> None:
    assert audit.current_actor() == audit.SYSTEM
    with audit.acting_as("operator"):
        assert audit.current_actor() == "operator"
        with audit.acting_as(audit.VOICE_AGENT):
            assert audit.current_actor() == audit.VOICE_AGENT
        assert audit.current_actor() == "operator"
    assert audit.current_actor() == audit.SYSTEM


async def test_events_and_task_resolution_record_who_acted(db) -> None:
    await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
    [task] = await open_tasks("INV-M")
    with audit.acting_as("operator"):
        resolved, _ = await service.resolve_task(task.id, "rejected", now=NOW)

    assert resolved.resolved_by == "operator"
    actors = [(e.event, e.actor) for e in await events(db, "INV-M")]
    assert actors[0][1] == audit.SYSTEM  # the sync, run outside any request
    assert actors[-1] == ("task:review_dispute:rejected", "operator")


async def test_tasks_closed_by_settlement_are_marked_as_the_workflows(db) -> None:
    await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
    with audit.acting_as("admin"):
        await service.record_payment("INV-M", Decimal("50000"), reference="UTR1", now=NOW)

    async with db.get_sessionmaker()() as session:
        [task] = (await session.execute(select(TaskRow))).scalars()
    assert (task.status, task.resolved_by) == ("done", audit.WORKFLOW)
    assert (await events(db, "INV-M"))[-1].actor == "admin"


async def test_bank_exception_records_who_dismissed_it(db) -> None:
    await service.sync_invoices([medium()], now=NOW)
    line = BankLine(
        utr="U-1",
        value_date=NOW.date(),
        amount=Decimal("123"),
        currency="INR",
        reference="TRANSFER",
        payer_name="Nobody Known",
    )
    report = await cash.ingest([line], source="feed", now=NOW)
    [result] = report.lines
    assert result.status == "unmatched"
    with audit.acting_as("operator"):
        dismissed = await cash.dismiss(result.transaction_id, note="Refund to issue", now=NOW)
    assert dismissed.resolved_by == "operator"
