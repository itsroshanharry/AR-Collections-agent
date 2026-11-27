"""Customer-level collections: the call prompt and the grounding check see the whole
account, not just the invoice the call is filed under."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from shared.eval.rubric import candidate_hallucinations
from shared.models import AccountItem
from tests.conftest import make_invoice


def account_invoice():
    return make_invoice(
        invoice_id="INV-101",
        amount=Decimal("50000.00"),
        other_open_invoices=[
            AccountItem(
                invoice_id="INV-117",
                amount=Decimal("120000.00"),
                due_date=date(2026, 7, 1),
                days_overdue=15,
            ),
        ],
    )


def test_single_invoice_context_is_unchanged() -> None:
    block = make_invoice().context_block()
    assert "other_overdue_invoices" not in block
    assert "account_total" not in block


def test_context_lists_the_rest_of_the_account_and_its_total() -> None:
    block = account_invoice().context_block()
    assert "INV-117: 120000.00 INR, due 2026-07-01, 15 days overdue" in block
    assert "account_total_overdue: 170000.00 INR" in block


def test_totals_are_never_summed_across_currencies() -> None:
    inv = make_invoice(
        amount=Decimal("100.00"),
        currency="USD",
        other_open_invoices=[
            AccountItem(
                invoice_id="INV-2",
                amount=Decimal("5000.00"),
                currency="inr",
                due_date=date(2026, 7, 1),
                days_overdue=3,
            ),
            AccountItem(
                invoice_id="INV-3",
                amount=Decimal("50.00"),
                currency="USD",
                due_date=date(2026, 7, 1),
                days_overdue=3,
            ),
        ],
    )
    assert inv.account_totals() == {"USD": Decimal("150.00"), "INR": Decimal("5000.00")}


def test_account_figures_are_quotable_and_others_are_not() -> None:
    inv = account_invoice()
    grounded = ["You have 170,000 rupees overdue: INV-101 and INV-117, which is 120000."]
    assert candidate_hallucinations(inv, grounded) == []
    invented = ["Your total is 175000 across both invoices."]
    assert candidate_hallucinations(inv, invented) == ["175000"]
