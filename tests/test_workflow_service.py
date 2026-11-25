"""The dunning workflow end to end against a real SQLite database, plus its API."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm.exc import StaleDataError

from ab_dashboard.db import InvoiceStateRow
from shared.models import CallOutcome, Customer, OutcomeType
from tests.conftest import make_invoice
from workflow import service
from workflow.policy import get_policy

IST = ZoneInfo("Asia/Kolkata")
# Tuesday 24 Nov 2026, 11:00 in India: inside calling hours.
NOW = datetime(2026, 11, 24, 11, 0, tzinfo=IST)
POLICY = get_policy()


@pytest.fixture
async def db(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    await db_module.init_db()
    yield db_module
    await db_module.dispose_engine()
    get_settings.cache_clear()


def medium(invoice_id="INV-M", customer="C-M", **kw):
    """30 days, ₹50k: medium tier, whose cadence opens with an email."""
    return make_invoice(
        invoice_id=invoice_id, customer=Customer(customer_id=customer, name="Medium Co"), **kw
    )


def high(invoice_id="INV-H", customer="C-H", **kw):
    """60 days: high tier, whose cadence opens with a call."""
    return make_invoice(
        invoice_id=invoice_id,
        customer=Customer(customer_id=customer, name="High Co"),
        days_overdue=60,
        **kw,
    )


async def row(db, invoice_id: str) -> InvoiceStateRow:
    async with db.get_sessionmaker()() as session:
        return await session.get(InvoiceStateRow, invoice_id)


async def open_tasks(invoice_id: str, task_type: str | None = None):
    return await service.list_tasks(invoice_id=invoice_id, status="open", task_type=task_type)


class TestSync:
    async def test_sync_is_idempotent(self, db) -> None:
        first = await service.sync_invoices([medium(), high()], now=NOW)
        again = await service.sync_invoices([medium(), high()], now=NOW)
        assert (first.created, again.created, again.already_present) == (2, 0, 2)

    async def test_paid_up_invoices_are_not_synced(self, db) -> None:
        report = await service.sync_invoices([medium(days_overdue=0)], now=NOW)
        assert report.created == 0

    async def test_ledger_dispute_goes_to_the_disputes_desk(self, db) -> None:
        report = await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
        assert report.disputed_on_arrival == 1
        r = await row(db, "INV-M")
        assert (r.state, r.next_action_at) == ("disputed", None)
        [task] = await open_tasks("INV-M")
        assert task.assignee == "disputes"


class TestTick:
    async def test_first_email_goes_out_once(self, db) -> None:
        await service.sync_invoices([medium()], now=NOW)
        first = await service.tick(now=NOW)
        second = await service.tick(now=NOW + timedelta(minutes=5))
        assert (first.emails_sent, second.emails_sent) == (1, 0)
        r = await row(db, "INV-M")
        assert (r.state, r.cadence_step, r.next_action_channel) == ("in_dunning", 1, "call")
        # Medium step 2 is a call 3 days later, inside calling hours.
        assert service.as_utc(r.next_action_at) == NOW + timedelta(days=3)

    async def test_calls_are_left_for_the_agent(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        report = await service.tick(now=NOW)
        assert (report.calls_due, report.emails_sent) == (1, 0)

    async def test_one_conversation_per_customer(self, db) -> None:
        two = [medium("INV-A", customer="C-1"), medium("INV-B", customer="C-1")]
        await service.sync_invoices(two, now=NOW)
        report = await service.tick(now=NOW)
        assert report.emails_sent == 1
        steps = {(await row(db, i)).cadence_step for i in ("INV-A", "INV-B")}
        assert steps == {0, 1}  # one emailed, the other still waiting its turn

    async def test_urgent_call_is_not_pre_empted_by_a_reminder_email(self, db) -> None:
        # Regression: found on the real dataset. A customer's low-priority invoice was
        # emailed in the same pass that their high-priority call came due, and the
        # minimum contact gap then blocked the call.
        await service.sync_invoices(
            [medium("INV-A", customer="C-1"), high("INV-Z", customer="C-1")], now=NOW
        )
        report = await service.tick(now=NOW)
        assert (report.emails_sent, report.calls_due) == (0, 1)
        assert [d.invoice.invoice_id for d in await service.due_calls(now=NOW)] == ["INV-Z"]

    async def test_lapsed_promise_is_broken_and_retiered(self, db) -> None:
        await service.sync_invoices([medium()], now=NOW)
        promise = NOW.date() + timedelta(days=1)
        await service.record_call_outcome(
            "INV-M",
            CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=promise),
            call_log_id="CALL-1",
            now=NOW,
        )
        later = NOW + timedelta(days=1 + POLICY.promise_grace_days + 1)
        report = await service.tick(now=later)

        assert report.promises_broken == 1
        r = await row(db, "INV-M")
        assert (r.state, r.broken_promises, r.tier) == ("in_dunning", 1, "high")
        assert await open_tasks("INV-M", "verify_payment") == []


class TestCallQueue:
    async def test_due_call_appears_in_hours(self, db) -> None:
        await service.sync_invoices([high(), medium()], now=NOW)
        queue = await service.due_calls(now=NOW)
        assert [d.invoice.invoice_id for d in queue] == ["INV-H"]

    async def test_no_calls_after_hours(self, db) -> None:
        late = datetime(2026, 11, 24, 21, 0, tzinfo=IST)
        await service.sync_invoices([high()], now=late)
        assert await service.due_calls(now=late) == []

    async def test_queue_uses_the_aged_balance(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        await service.record_payment("INV-H", Decimal("20000"), now=NOW)
        [due] = await service.due_calls(now=NOW)
        assert due.invoice.amount == Decimal("30000.00")
        week_later = await service.due_calls(now=NOW + timedelta(days=7))
        assert week_later[0].invoice.days_overdue == 67


class TestOutcomes:
    async def test_promise_opens_one_verify_task_even_if_reprocessed(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        outcome = CallOutcome(
            outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=date(2026, 11, 30)
        )
        await service.record_call_outcome("INV-H", outcome, call_log_id="CALL-9", now=NOW)
        await service.record_call_outcome("INV-H", outcome, call_log_id="CALL-9", now=NOW)
        assert len(await open_tasks("INV-H", "verify_payment")) == 1
        assert (await row(db, "INV-H")).state == "promised"

    async def test_unsynced_invoice_joins_the_workflow_on_its_first_call(self, db) -> None:
        report = await service.record_call_outcome(
            "INV-H",
            CallOutcome(outcome=OutcomeType.NO_ANSWER),
            invoice=high(),
            now=NOW,
        )
        assert report.state == "in_dunning"

    async def test_unknown_invoice_without_record_is_rejected(self, db) -> None:
        with pytest.raises(service.NotFoundError):
            await service.record_call_outcome(
                "NOPE", CallOutcome(outcome=OutcomeType.NO_ANSWER), now=NOW
            )

    async def test_live_capture_disagreeing_with_extraction_opens_qa_task(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        report = await service.record_call_outcome(
            "INV-H",
            CallOutcome(outcome=OutcomeType.UNCLEAR),
            captured={"outcome": "promise_to_pay", "promise_to_pay_date": "2026-11-30"},
            call_log_id="CALL-2",
            now=NOW,
        )
        assert "promise_to_pay" in report.discrepancy
        [task] = await open_tasks("INV-H", "review_call")
        assert task.assignee == "qa"

    async def test_matching_capture_opens_nothing(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        report = await service.record_call_outcome(
            "INV-H",
            CallOutcome(
                outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=date(2026, 11, 30)
            ),
            captured={"outcome": "promise_to_pay", "promise_to_pay_date": "2026-11-30"},
            now=NOW,
        )
        assert report.discrepancy is None

    async def test_a_call_counts_against_the_contact_caps(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        await service.record_call_outcome(
            "INV-H", CallOutcome(outcome=OutcomeType.NO_ANSWER), now=NOW
        )
        assert await service.due_calls(now=NOW + timedelta(hours=1)) == []


class TestPaymentsAndTasks:
    async def test_full_payment_closes_invoice_and_tasks(self, db) -> None:
        await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
        r = await service.record_payment("INV-M", Decimal("50000"), reference="UTR1", now=NOW)
        assert (r.state, r.closed_reason, r.amount_outstanding) == ("closed", "paid", "0.00")
        assert await open_tasks("INV-M") == []

    async def test_payment_on_closed_invoice_is_rejected(self, db) -> None:
        await service.sync_invoices([medium()], now=NOW)
        await service.record_payment("INV-M", Decimal("50000"), now=NOW)
        with pytest.raises(service.TransitionError):
            await service.record_payment("INV-M", Decimal("1"), now=NOW)

    async def test_rejected_dispute_resumes_dunning(self, db) -> None:
        await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
        [task] = await open_tasks("INV-M")
        task, r = await service.resolve_task(task.id, "rejected", note="POD on file", now=NOW)
        assert (task.status, task.resolution) == ("done", "rejected")
        assert (r.state, r.next_action_channel) == ("in_dunning", "email")

    async def test_task_cannot_be_resolved_twice(self, db) -> None:
        await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
        [task] = await open_tasks("INV-M")
        await service.resolve_task(task.id, "upheld", now=NOW)
        with pytest.raises(service.TransitionError):
            await service.resolve_task(task.id, "rejected", now=NOW)

    async def test_do_not_call_turns_the_call_into_email(self, db) -> None:
        await service.sync_invoices([high()], now=NOW)
        r = await service.set_do_not_call("INV-H", True, now=NOW)
        assert r.next_action_channel == "email"
        assert await service.due_calls(now=NOW) == []

    async def test_timeline_records_every_transition_in_order(self, db) -> None:
        await service.sync_invoices([medium(dispute_flag=True)], now=NOW)
        [task] = await open_tasks("INV-M")
        await service.resolve_task(task.id, "rejected", now=NOW)
        detail = await service.invoice_detail("INV-M", now=NOW)
        assert [e.event for e in detail.events] == [
            "synced",
            "disputed_on_sync",
            "task:review_dispute:rejected",
        ]


class TestConcurrency:
    async def test_stale_write_is_refused(self, db) -> None:
        await service.sync_invoices([medium()], now=NOW)
        maker = db.get_sessionmaker()
        async with maker() as a, maker() as b:
            row_a = await a.get(InvoiceStateRow, "INV-M")
            row_b = await b.get(InvoiceStateRow, "INV-M")
            row_a.cadence_step = 2
            await a.commit()
            row_b.cadence_step = 3
            with pytest.raises(StaleDataError):
                await b.commit()


class TestApi:
    @pytest.fixture
    def client(self, database_url, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
        import ab_dashboard.db as db_module
        import data.generate_dataset as dataset
        from shared.config import get_settings

        monkeypatch.setattr(dataset, "load_invoices", lambda: [high(), medium()])
        get_settings.cache_clear()
        db_module._engine = None
        db_module._sessionmaker = None

        from fastapi.testclient import TestClient

        from ab_dashboard.main import app

        with TestClient(app) as client:
            yield client
        db_module._engine = None
        db_module._sessionmaker = None
        get_settings.cache_clear()

    def test_outcome_to_task_to_resolution(self, client) -> None:
        assert client.post("/dunning/sync").json()["created"] == 2

        resp = client.post(
            "/dunning/invoices/INV-H/outcomes",
            json={"outcome": "dispute_raised", "dispute_reason": "wrong rate"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "disputed"

        [task] = client.get("/tasks", params={"assignee": "disputes"}).json()
        assert task["allowed_resolutions"] == ["rejected", "upheld"]

        bad = client.patch(f"/tasks/{task['id']}", json={"resolution": "maybe"})
        assert bad.status_code == 409

        ok = client.patch(f"/tasks/{task['id']}", json={"resolution": "upheld"})
        assert ok.json()["status"] == "done"

        detail = client.get("/dunning/invoices/INV-H").json()
        assert detail["state"]["state"] == "closed"
        assert [e["event"] for e in detail["timeline"]][-1] == "task:review_dispute:upheld"

    def test_unknown_invoice_is_404(self, client) -> None:
        assert client.get("/dunning/invoices/NOPE").status_code == 404
        resp = client.post("/dunning/invoices/NOPE/payments", json={"amount": "1"})
        assert resp.status_code == 404

    def test_tick_and_queue(self, client) -> None:
        client.post("/dunning/sync")
        assert client.post("/dunning/tick").status_code == 200
        assert client.get("/dunning/queue").status_code == 200
