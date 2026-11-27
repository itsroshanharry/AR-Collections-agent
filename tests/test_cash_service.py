"""Cash application end to end: bank lines in, invoices and their follow-ups settled."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from shared.models import CallOutcome, Customer, OutcomeType
from tests.conftest import make_invoice
from tests.test_workflow_service import NOW, open_tasks, row
from workflow import cash, service
from workflow.cash_application import BankLine
from workflow.state_machine import TransitionError

D = Decimal


def acme(invoice_id, amount="50000.00", **kw):
    return make_invoice(
        invoice_id=invoice_id,
        customer=Customer(customer_id="C-1", name="Acme Pvt Ltd"),
        amount=D(amount),
        **kw,
    )


def credit(amount, reference="", payer="", utr="UTR-0001", currency="INR"):
    return BankLine(
        utr=utr,
        value_date=date(2026, 11, 24),
        amount=D(amount),
        currency=currency,
        reference=reference,
        payer_name=payer,
    )


async def ingest(*lines):
    return await cash.ingest(list(lines), now=NOW)


class TestAutomaticMatching:
    async def test_clean_payment_closes_the_invoice_and_its_follow_ups(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        await service.record_call_outcome(
            "INV-1",
            CallOutcome(outcome=OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=date(2026, 11, 28)),
            call_log_id="CALL-1",
            now=NOW,
        )
        report = await ingest(credit("50000", "NEFT/INV-1/ACME"))

        assert (report.matched, report.invoices_settled, report.auto_match_rate) == (1, 1, 1.0)
        r = await row(db, "INV-1")
        assert (r.state, r.closed_reason, r.amount_outstanding) == ("closed", "paid", "0.00")
        # Nobody has to confirm the promised payment arrived: the feed did.
        assert await open_tasks("INV-1") == []
        detail = await service.invoice_detail("INV-1", now=NOW)
        assert detail.events[-1].detail["match_rule"] == "invoice_reference"
        assert detail.events[-1].detail["bank_transaction"] == report.lines[0].transaction_id

    async def test_replaying_the_feed_applies_nothing_twice(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        await ingest(credit("20000", "INV-1"))
        again = await ingest(credit("20000", "INV-1"))
        assert (again.duplicates, again.matched) == (1, 0)
        assert (await row(db, "INV-1")).amount_outstanding == "30000.00"

    async def test_part_payment_reduces_the_balance_and_dunning_continues(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("20000", "INV-1"))
        assert (report.invoices_settled, report.invoices_part_paid) == (0, 1)
        r = await row(db, "INV-1")
        assert (r.state, r.amount_outstanding) == ("new", "30000.00")
        [due] = await service.due_calls(now=NOW) or [None]
        assert due is None or due.invoice.amount == D("30000.00")

    async def test_bank_charges_are_written_off(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        await ingest(credit("49975", "INV-1"))
        r = await row(db, "INV-1")
        assert (r.state, r.amount_outstanding) == ("closed", "0.00")
        detail = await service.invoice_detail("INV-1", now=NOW)
        assert detail.events[-1].detail["written_off"] == "25.00"

    async def test_account_payment_settles_every_invoice(self, db) -> None:
        await service.sync_invoices([acme("INV-1"), acme("INV-2", "20000.00")], now=NOW)
        report = await ingest(credit("70000", "PAYMENT", payer="ACME PRIVATE LIMITED"))
        assert report.lines[0].rule == "customer_account"
        assert report.invoices_settled == 2
        for invoice_id in ("INV-1", "INV-2"):
            assert (await row(db, invoice_id)).state == "closed"


class TestExceptions:
    async def test_unrecognised_payment_waits_for_a_person(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("12345", "TRANSFER", payer="Unknown"))
        assert (report.unmatched, report.auto_match_rate) == (1, 0.0)
        [txn] = await cash.list_transactions(status="unmatched")
        assert "not recognised" in txn.reason
        assert (await row(db, "INV-1")).amount_outstanding == "50000.00"

    async def test_person_applies_an_exception(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("50000", "TRANSFER", payer="A Sharma"))
        txn_id = report.lines[0].transaction_id

        txn = await cash.apply_manually(txn_id, [("INV-1", D("50000"))], note="director paid")

        assert (txn.status, txn.allocations[0]["settles"]) == ("applied", True)
        assert (await row(db, "INV-1")).state == "closed"
        with pytest.raises(TransitionError):
            await cash.apply_manually(txn_id, [("INV-1", D("1"))])

    async def test_overpayment_part_can_be_left_unapplied_for_refund(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("50500", "INV-1"))
        assert "overpayment" in report.lines[0].reason
        txn = await cash.apply_manually(
            report.lines[0].transaction_id, [("INV-1", D("50000"))], note="refund 500"
        )
        assert txn.resolution_note == "refund 500 (unapplied: 500.00)"

    @pytest.mark.parametrize(
        ("allocations", "error"),
        [
            ([("INV-1", D("60000"))], "more than the"),
            ([("INV-1", D("10")), ("INV-1", D("10"))], "once"),
            ([("INV-1", D("0"))], "positive"),
            ([], "at least one"),
        ],
    )
    async def test_bad_manual_allocations_are_refused(self, db, allocations, error) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("12345", payer="Unknown"))
        with pytest.raises(ValueError, match=error):
            await cash.apply_manually(report.lines[0].transaction_id, allocations)

    async def test_dismissal_needs_a_reason(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        report = await ingest(credit("12345", payer="Unknown"))
        txn_id = report.lines[0].transaction_id
        with pytest.raises(ValueError):
            await cash.dismiss(txn_id, note=" ")
        txn = await cash.dismiss(txn_id, note="transfer from our own savings account")
        assert txn.status == "dismissed"


class TestAlreadyPaid:
    already_paid = CallOutcome(outcome=OutcomeType.ALREADY_PAID)

    async def test_claim_then_payment_arrives_without_an_invoice_reference(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        call = await service.record_call_outcome(
            "INV-1",
            self.already_paid,
            captured={"outcome": "already_paid", "payment_reference": "N2478119"},
            call_log_id="CALL-1",
            now=NOW,
        )
        assert (call.state, call.payment_verified_by) == ("paid_unverified", None)
        assert len(await open_tasks("INV-1", "reconcile_payment")) == 1

        report = await ingest(credit("50000", "TRANSFER", payer="A Sharma", utr="N2478119"))

        assert report.lines[0].rule == "customer_claim"
        assert (await row(db, "INV-1")).state == "closed"
        assert await open_tasks("INV-1") == []

    async def test_payment_already_in_the_queue_is_found_by_the_claim(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        await ingest(credit("50000", "TRANSFER", payer="A Sharma", utr="N2478119"))

        call = await service.record_call_outcome(
            "INV-1",
            self.already_paid,
            captured={"outcome": "already_paid", "payment_reference": "n2478119"},
            now=NOW,
        )

        assert call.state == "closed"
        assert call.payment_verified_by is not None
        assert await open_tasks("INV-1") == []
        assert await cash.list_transactions(status="unmatched") == []

    async def test_claim_without_a_payment_leaves_the_task_for_a_person(self, db) -> None:
        await service.sync_invoices([acme("INV-1")], now=NOW)
        await ingest(credit("999", "TRANSFER", payer="A Sharma", utr="N2478119"))
        call = await service.record_call_outcome(
            "INV-1",
            self.already_paid,
            captured={"outcome": "already_paid", "payment_reference": "N2478119"},
            now=NOW,
        )
        # A part payment under the quoted reference is applied, but does not settle it.
        assert call.state == "paid_unverified"
        assert (await row(db, "INV-1")).amount_outstanding == "49001.00"
        assert len(await open_tasks("INV-1", "reconcile_payment")) == 1


class TestStatement:
    def test_parses_credits_and_skips_debits(self) -> None:
        text = (
            "value_date,amount,currency,reference,payer_name,utr\n"
            '2026-11-24,"50,000.00",inr,NEFT INV-1,ACME,U1\n'
            "2026-11-24,-200,INR,BANK FEE,,U2\n"
        )
        [line] = cash.parse_statement(text)
        assert (line.amount, line.currency, line.utr) == (D("50000.00"), "INR", "U1")

    def test_missing_columns(self) -> None:
        with pytest.raises(cash.StatementError, match="utr"):
            cash.parse_statement("value_date,amount,currency,reference,payer_name\n")

    def test_bad_line_is_reported_by_number(self) -> None:
        text = "value_date,amount,currency,reference,payer_name,utr\n24/11/2026,1,INR,,,U1\n"
        with pytest.raises(cash.StatementError, match="line 2"):
            cash.parse_statement(text)
