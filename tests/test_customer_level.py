"""Customer-level collections: one conversation per account, not per invoice."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from shared.models import CallOutcome, OutcomeType, RiskTier
from tests.test_workflow_service import NOW, high, medium, open_tasks, row
from workflow import service
from workflow.policy import Channel
from workflow.state_machine import (
    DunningState,
    Snapshot,
    TransitionError,
    on_account_contact,
    promise_covers_account,
)

PROMISE = date(2026, 11, 30)


def account():
    """One customer, three overdue invoices: 60 days ₹50k, and two at 30 days ₹50k."""
    return [
        high("INV-A", customer="C-1"),
        medium("INV-B", customer="C-1"),
        medium("INV-C", customer="C-1"),
    ]


class TestRules:
    def snap(self, state=DunningState.IN_DUNNING) -> Snapshot:
        return Snapshot(
            invoice_id="INV-B",
            state=state,
            tier=RiskTier.MEDIUM,
            cadence_tier=RiskTier.MEDIUM,
            cadence_step=1,
            cadence_started_at=NOW,
        )

    def test_covered_invoice_records_the_contact_without_advancing(self) -> None:
        t = on_account_contact(self.snap(), Channel.EMAIL, NOW, lead_invoice_id="INV-A")
        assert (t.snapshot.cadence_step, t.snapshot.last_contact_at) == (1, NOW)
        assert t.event == "account_contact:email"
        assert t.detail == {"lead_invoice": "INV-A"}

    def test_paused_invoice_is_not_covered(self) -> None:
        with pytest.raises(TransitionError):
            on_account_contact(
                self.snap(DunningState.DISPUTED), Channel.CALL, NOW, lead_invoice_id="INV-A"
            )

    @pytest.mark.parametrize(
        ("amount", "totals", "covers"),
        [
            (None, {"INR": Decimal("150000")}, True),
            (Decimal("150000"), {"INR": Decimal("150000")}, True),
            (Decimal("50000"), {"INR": Decimal("150000")}, False),
            (Decimal("999999"), {"INR": Decimal("1"), "USD": Decimal("1")}, False),
        ],
    )
    def test_which_promises_cover_the_account(self, amount, totals, covers) -> None:
        outcome = CallOutcome(
            outcome=OutcomeType.PROMISE_TO_PAY,
            promise_to_pay_date=PROMISE,
            promised_amount=amount,
        )
        assert promise_covers_account(outcome, totals) is covers

    def test_partial_payment_plan_never_covers_the_account(self) -> None:
        outcome = CallOutcome(outcome=OutcomeType.PARTIAL_PAYMENT, promise_to_pay_date=PROMISE)
        assert not promise_covers_account(outcome, {"INR": Decimal("1")})


class TestQueue:
    async def test_one_call_per_customer_listing_the_account(self, db) -> None:
        await service.sync_invoices([*account(), high("INV-Z", customer="C-2")], now=NOW)
        # Make every invoice of C-1 want a call now.
        for invoice_id in ("INV-B", "INV-C"):
            async with db.get_sessionmaker()() as session:
                r = await session.get(db.InvoiceStateRow, invoice_id)
                r.next_action_channel, r.next_action_at = "call", NOW
                r.cadence_tier, r.tier = "high", "high"
                await session.commit()

        queue = await service.due_calls(now=NOW)

        assert sorted(d.invoice.customer.customer_id for d in queue) == ["C-1", "C-2"]
        [c1] = [d for d in queue if d.invoice.customer.customer_id == "C-1"]
        assert c1.invoice.invoice_id == "INV-A"  # the most urgent leads
        assert [i.invoice_id for i in c1.invoice.other_open_invoices] == ["INV-B", "INV-C"]
        assert c1.invoice.account_totals() == {"INR": Decimal("150000.00")}

    async def test_paused_invoices_are_left_out_of_the_conversation(self, db) -> None:
        invoices = account()
        invoices[2] = medium("INV-C", customer="C-1", dispute_flag=True)
        await service.sync_invoices(invoices, now=NOW)
        [call] = await service.due_calls(now=NOW)
        assert [i.invoice_id for i in call.invoice.other_open_invoices] == ["INV-B"]


class TestReminders:
    async def test_one_reminder_covers_every_invoice_on_the_account(self, db) -> None:
        await service.sync_invoices(
            [medium("INV-B", customer="C-1"), medium("INV-C", customer="C-1")], now=NOW
        )
        report = await service.tick(now=NOW)
        assert (report.emails_sent, report.covered_by_account_contact) == (1, 1)

        lead, covered = await row(db, "INV-B"), await row(db, "INV-C")
        assert lead.cadence_step == 1
        assert (covered.state, covered.cadence_step) == ("in_dunning", 0)
        assert service.as_utc(covered.last_contact_at) == NOW
        detail = await service.invoice_detail("INV-C", now=NOW)
        assert detail.events[-1].event == "account_contact:email"
        assert detail.events[-1].detail["lead_invoice"] == "INV-B"

    async def test_covered_invoice_does_not_send_its_own_reminder_next(self, db) -> None:
        await service.sync_invoices(
            [medium("INV-B", customer="C-1"), medium("INV-C", customer="C-1")], now=NOW
        )
        await service.tick(now=NOW)
        report = await service.tick(now=NOW + timedelta(hours=21))
        assert report.emails_sent == 0


class TestAccountCalls:
    async def call(self, outcome: CallOutcome, **kw):
        [due] = await service.due_calls(now=NOW)
        return await service.record_call_outcome(
            due.invoice.invoice_id,
            outcome,
            invoice=due.invoice,
            call_log_id="CALL-1",
            now=NOW,
            **kw,
        )

    async def test_promise_for_the_account_puts_every_invoice_on_it(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        report = await self.call(
            CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=PROMISE)
        )
        assert report.promise_covers_account
        assert report.account_invoices == ["INV-B", "INV-C"]
        for invoice_id in ("INV-A", "INV-B", "INV-C"):
            r = await row(db, invoice_id)
            assert (r.state, r.promise_to_pay_date) == ("promised", PROMISE)
            # Each invoice gets its own verify task, so each closes on its own payment.
            assert len(await open_tasks(invoice_id, "verify_payment")) == 1

    async def test_reprocessing_an_account_call_opens_no_duplicate_tasks(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        outcome = CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=PROMISE)
        [due] = await service.due_calls(now=NOW)
        for _ in range(2):
            await service.record_call_outcome(
                "INV-A", outcome, also_covers=["INV-B", "INV-C"], call_log_id="CALL-1", now=NOW
            )
        assert len(await open_tasks("INV-B", "verify_payment")) == 1

    async def test_promise_for_less_than_the_account_stays_with_the_lead(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        report = await self.call(
            CallOutcome(
                outcome=OutcomeType.PROMISE_TO_PAY,
                promise_to_pay_date=PROMISE,
                promised_amount=Decimal("50000"),
            )
        )
        assert not report.promise_covers_account
        assert (await row(db, "INV-A")).state == "promised"
        other = await row(db, "INV-B")
        assert (other.state, other.promise_to_pay_date) == ("in_dunning", None)
        assert service.as_utc(other.last_contact_at) == NOW

    async def test_dispute_stays_with_the_invoice_it_was_raised_on(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        await self.call(CallOutcome(outcome=OutcomeType.DISPUTE_RAISED, dispute_reason="rate"))
        assert (await row(db, "INV-A")).state == "disputed"
        assert (await row(db, "INV-B")).state == "in_dunning"
        assert await open_tasks("INV-B") == []

    async def test_hand_recorded_call_can_cover_the_account(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        report = await service.record_call_outcome(
            "INV-A",
            CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=PROMISE),
            cover_account=True,
            now=NOW,
        )
        assert report.account_invoices == ["INV-B", "INV-C"]

    async def test_single_invoice_call_touches_nothing_else(self, db) -> None:
        await service.sync_invoices(account(), now=NOW)
        report = await service.record_call_outcome(
            "INV-A",
            CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=PROMISE),
            now=NOW,
        )
        assert report.account_invoices == []
        assert (await row(db, "INV-B")).state == "new"


class TestAccountView:
    async def test_account_totals_separate_dunning_from_paused(self, db) -> None:
        invoices = account()
        invoices[2] = medium("INV-C", customer="C-1", dispute_flag=True)
        await service.sync_invoices(invoices, now=NOW)
        await service.record_payment("INV-B", Decimal("20000"), now=NOW)

        acct = await service.customer_account("C-1", now=NOW)

        assert [r.invoice_id for r, _ in acct.invoices] == ["INV-A", "INV-B", "INV-C"]
        assert acct.overdue_in_dunning == {"INR": Decimal("80000.00")}
        assert acct.outstanding == {"INR": Decimal("130000.00")}
        assert [t.task_type for t in acct.open_tasks] == ["review_dispute"]

    async def test_unknown_customer(self, db) -> None:
        with pytest.raises(service.NotFoundError):
            await service.customer_account("NOPE", now=NOW)
