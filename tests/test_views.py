"""Read models for the operator console: the overview and the customer list."""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.models import Customer
from tests.conftest import make_invoice
from tests.test_workflow_service import NOW
from workflow import cash, service, views
from workflow.cash_application import BankLine


def inv(invoice_id, customer, name, days=30, amount="50000", currency="INR", **kw):
    return make_invoice(
        invoice_id=invoice_id,
        customer=Customer(customer_id=customer, name=name),
        days_overdue=days,
        amount=Decimal(amount),
        currency=currency,
        **kw,
    )


LEDGER = [
    inv("INV-1", "C-1", "Acme Pvt Ltd"),
    inv("INV-2", "C-1", "Acme Pvt Ltd", days=100),
    inv("INV-3", "C-2", "Bharat Traders", amount="1000", currency="USD"),
    inv("INV-4", "C-3", "Zenith Foods", dispute_flag=True),
]


async def credit(utr, amount, reference="", payer=""):
    from datetime import date

    await cash.ingest(
        [BankLine(utr, date(2026, 11, 24), Decimal(amount), "INR", reference, payer)], now=NOW
    )


class TestSummary:
    async def test_overview_figures(self, db) -> None:
        await service.sync_invoices(LEDGER, now=NOW)
        await credit("U1", "50000", "INV-1")
        await credit("U2", "777", payer="Nobody")

        s = await views.summary(now=NOW)

        assert s.invoices == 4
        assert s.invoices_by_state["closed"] == 1
        assert sum(s.invoices_by_tier.values()) == 3  # closed invoices are not on the book
        # 1000 USD is normalised to INR so tiers compare.
        assert s.outstanding_inr_by_tier["medium"] == Decimal("138000.00")
        assert s.open_tasks_by_team == {"disputes": 1}
        assert s.bank_transactions_by_status == {"matched": 1, "unmatched": 1}
        assert s.auto_match_rate == 0.5

    async def test_empty_book(self, db) -> None:
        s = await views.summary(now=NOW)
        assert (s.invoices, s.auto_match_rate, s.customers_with_calls_due) == (0, None, 0)


class TestCustomers:
    async def test_largest_exposure_first_with_worst_tier(self, db) -> None:
        await service.sync_invoices(LEDGER, now=NOW)
        page = await views.list_customers(now=NOW)
        assert [c.customer_id for c in page.customers] == ["C-1", "C-2", "C-3"]
        acme = page.customers[0]
        assert (acme.invoices, acme.open_invoices, acme.tier) == (2, 2, "critical")
        assert acme.outstanding == {"INR": Decimal("100000.00")}

    async def test_search_filter_and_page(self, db) -> None:
        await service.sync_invoices(LEDGER, now=NOW)
        assert [c.customer_id for c in (await views.list_customers(q="bharat")).customers] == [
            "C-2"
        ]
        with_tasks = await views.list_customers(has_open_tasks=True)
        assert [c.customer_id for c in with_tasks.customers] == ["C-3"]
        page = await views.list_customers(sort="name", limit=1, offset=1)
        assert (page.total, page.customers[0].customer_name) == (3, "Bharat Traders")

    async def test_settled_customer_has_no_tier(self, db) -> None:
        await service.sync_invoices(LEDGER, now=NOW)
        await credit("U1", "1000", "INV-3")  # INR credit against a USD invoice: unmatched
        await service.record_payment("INV-4", Decimal("50000"), now=NOW)
        [zenith] = (await views.list_customers(q="zenith")).customers
        assert (zenith.tier, zenith.open_invoices, zenith.outstanding) == (None, 0, {})

    async def test_unknown_sort(self, db) -> None:
        with pytest.raises(ValueError):
            await views.list_customers(sort="vibes")
