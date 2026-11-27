"""The cash-application matcher: which bank lines are applied automatically, and why
the rest are left for a person."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from workflow.cash_application import (
    BankLine,
    OpenItem,
    identify_payer,
    match,
    referenced_invoices,
)

D = Decimal
TOL = D("50")


def item(invoice_id, amount, customer="C-1", name="Acme Pvt Ltd", currency="INR", due=1):
    return OpenItem(
        invoice_id=invoice_id,
        customer_id=customer,
        customer_name=name,
        currency=currency,
        outstanding=D(amount),
        due_date=date(2026, 6, due),
    )


def line(amount, reference="", payer="", currency="INR", utr="UTR1"):
    return BankLine(
        utr=utr,
        value_date=date(2026, 11, 24),
        amount=D(amount),
        currency=currency,
        reference=reference,
        payer_name=payer,
    )


ITEMS = [
    item("INV-100", "50000"),
    item("INV-101", "20000", due=2),
    item("INV-200", "50000", customer="C-2", name="Bharat Traders"),
]


class TestReferences:
    @pytest.mark.parametrize(
        "narrative",
        ["NEFT/INV-100/ACME", "inv 100 payment", "INV100", "RTGS-INV-100-AUG"],
    )
    def test_invoice_found_despite_bank_formatting(self, narrative) -> None:
        assert referenced_invoices(narrative, ["INV-100", "INV-1000"]) == ["INV-100"]

    def test_prefix_of_a_longer_id_is_not_a_match(self) -> None:
        assert referenced_invoices("INV-1000", ["INV-100"]) == []


class TestPayer:
    def test_bank_spelling_of_the_ledger_name(self) -> None:
        assert identify_payer(line("1", payer="ACME PRIVATE LIMITED"), ITEMS) == "C-1"

    def test_customer_id_in_the_narrative(self) -> None:
        assert identify_payer(line("1", reference="C-2 settlement"), ITEMS) == "C-2"

    def test_unknown_payer(self) -> None:
        assert identify_payer(line("1", payer="Someone Else"), ITEMS) is None


class TestMatch:
    def test_clean_payment_against_a_named_invoice_settles_it(self) -> None:
        r = match(line("50000", "NEFT INV-100"), ITEMS, tolerance=TOL)
        assert (r.matched, r.rule, r.customer_id) == (True, "invoice_reference", "C-1")
        assert [(a.invoice_id, a.amount, a.settles) for a in r.allocations] == [
            ("INV-100", D("50000"), True)
        ]

    def test_bank_charges_within_tolerance_still_settle(self) -> None:
        [a] = match(line("49975", "INV-100"), ITEMS, tolerance=TOL).allocations
        assert (a.amount, a.settles) == (D("49975"), True)

    def test_part_payment_is_applied_and_leaves_the_rest_open(self) -> None:
        r = match(line("20000", "INV-100"), ITEMS, tolerance=TOL)
        [a] = r.allocations
        assert (r.matched, a.amount, a.settles) == (True, D("20000"), False)
        assert "30000.00 INR left" in r.reason

    def test_overpayment_goes_to_a_person(self) -> None:
        r = match(line("50010", "INV-100"), ITEMS, tolerance=TOL)
        assert not r.matched
        assert "overpayment" in r.reason
        assert r.customer_id == "C-1"

    def test_wrong_currency_goes_to_a_person(self) -> None:
        r = match(line("600", "INV-100", currency="USD"), ITEMS, tolerance=TOL)
        assert not r.matched and "currency" in r.reason

    def test_several_named_invoices_paid_in_full(self) -> None:
        r = match(line("70000", "INV-100, INV-101"), ITEMS, tolerance=TOL)
        assert r.rule == "invoice_references"
        assert {a.invoice_id: a.amount for a in r.allocations} == {
            "INV-100": D("50000"),
            "INV-101": D("20000"),
        }
        assert all(a.settles for a in r.allocations)

    def test_several_named_invoices_short_paid_goes_to_a_person(self) -> None:
        r = match(line("60000", "INV-100 INV-101"), ITEMS, tolerance=TOL)
        assert not r.matched and r.invoice_ids == ("INV-100", "INV-101")

    def test_recognised_payer_paying_one_invoices_exact_balance(self) -> None:
        r = match(line("20000", payer="ACME PVT. LTD."), ITEMS, tolerance=TOL)
        assert (r.rule, [a.invoice_id for a in r.allocations]) == ("customer_amount", ["INV-101"])

    def test_recognised_payer_clearing_the_whole_account(self) -> None:
        r = match(line("69980", payer="Acme"), ITEMS, tolerance=TOL)
        assert r.rule == "customer_account"
        assert r.applied == D("69980")
        assert all(a.settles for a in r.allocations)

    def test_amount_fitting_two_invoices_equally_is_ambiguous(self) -> None:
        items = [item("INV-1", "500"), item("INV-2", "500", due=2)]
        r = match(line("500", payer="Acme"), items, tolerance=TOL)
        assert not r.matched and "INV-1, INV-2" in r.reason

    def test_recognised_payer_odd_amount_names_the_likely_customer(self) -> None:
        r = match(line("12345", payer="Bharat Traders"), ITEMS, tolerance=TOL)
        assert (r.matched, r.customer_id) == (False, "C-2")

    def test_nothing_to_go_on(self) -> None:
        r = match(line("12345", "TRANSFER", payer="Unknown"), ITEMS, tolerance=TOL)
        assert (r.matched, r.customer_id) == (False, None)
        assert "not recognised" in r.reason

    def test_payment_for_an_already_settled_invoice_is_flagged(self) -> None:
        r = match(
            line("50000", "INV-999"),
            ITEMS,
            tolerance=TOL,
            closed_invoice_ids=frozenset({"INV-999"}),
        )
        assert not r.matched and "duplicate" in r.reason

    def test_zero_tolerance_policy_does_not_settle_short_payments(self) -> None:
        [a] = match(line("49975", "INV-100"), ITEMS).allocations
        assert not a.settles
