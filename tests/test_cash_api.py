"""The simulated bank feed and the cash-application API."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from shared.models import Customer
from tests.conftest import make_invoice
from tests.test_workflow_service import NOW
from workflow import cash, service
from workflow.bank_feed import simulate_feed
from workflow.cash_application import OpenItem, match
from workflow.policy import get_policy


def ledger():
    """Four single-invoice customers and one with an account of two."""
    invoices = [
        make_invoice(
            invoice_id=f"INV-{n}",
            customer=Customer(customer_id=f"C-{n}", name=f"Customer {n} Pvt Ltd"),
            amount=Decimal(10_000 * n),
        )
        for n in range(1, 7)
    ]
    acct = Customer(customer_id="C-9", name="Account Holder Ltd")
    invoices += [
        make_invoice(invoice_id="INV-90", customer=acct, amount=Decimal("1000")),
        make_invoice(invoice_id="INV-91", customer=acct, amount=Decimal("2000")),
    ]
    return invoices


def items():
    return [
        OpenItem(
            invoice_id=i.invoice_id,
            customer_id=i.customer.customer_id,
            customer_name=i.customer.name,
            currency=i.currency,
            outstanding=i.amount,
            due_date=i.due_date,
        )
        for i in ledger()
    ]


class TestSimulatedFeed:
    def test_a_day_shows_every_kind_of_match_and_exception(self) -> None:
        lines = simulate_feed(items(), today=date(2026, 11, 24), count=8, seed=7)
        tol = get_policy().tolerance("INR")
        results = [match(line, items(), tolerance=tol) for line in lines]
        rules = Counter(r.rule for r in results)
        assert rules["invoice_reference"] >= 4  # clean, part, bank charges, no separator
        assert rules["customer_account"] == 1
        assert rules["none"] == 2  # unknown payer, overpayment

    def test_deterministic_for_a_seed(self) -> None:
        a = simulate_feed(items(), today=date(2026, 11, 24), seed=3)
        b = simulate_feed(items(), today=date(2026, 11, 24), seed=3)
        assert a == b

    def test_never_pays_the_same_invoice_twice(self) -> None:
        lines = simulate_feed(items(), today=date(2026, 11, 24), count=50, seed=1)
        named = [line.reference for line in lines if "INV" in line.reference]
        assert len(named) == len(set(named))

    async def test_simulate_runs_against_the_workflow(self, db) -> None:
        await service.sync_invoices(ledger(), now=NOW)
        report = await cash.simulate(count=8, seed=7, now=NOW)
        assert report.received == 8
        assert report.matched >= 5 and report.unmatched == 2
        assert report.invoices_settled >= 4


class TestApi:
    @pytest.fixture
    def client(self, database_url, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
        import ab_dashboard.db as db_module
        import data.generate_dataset as dataset
        from shared.config import get_settings

        monkeypatch.setattr(dataset, "load_invoices", ledger)
        get_settings.cache_clear()
        db_module._engine = None
        db_module._sessionmaker = None

        from fastapi.testclient import TestClient

        from ab_dashboard.main import app

        with TestClient(app) as client:
            client.post("/dunning/sync")
            yield client
        db_module._engine = None
        db_module._sessionmaker = None
        get_settings.cache_clear()

    def credit(self, utr, amount, reference="", payer=""):
        return {
            "utr": utr,
            "value_date": "2026-11-24",
            "amount": amount,
            "reference": reference,
            "payer_name": payer,
        }

    def test_feed_matches_and_queues(self, client) -> None:
        resp = client.post(
            "/cash/transactions",
            json=[
                self.credit("U1", "10000", "NEFT/INV-1"),
                self.credit("U2", "777", "TRANSFER", "Nobody"),
            ],
        )
        body = resp.json()
        assert (body["matched"], body["unmatched"], body["auto_match_rate"]) == (1, 1, 0.5)
        assert client.get("/dunning/invoices/INV-1").json()["state"]["state"] == "closed"

        [queued] = client.get("/cash/transactions", params={"status": "unmatched"}).json()
        assert queued["utr"] == "U2"

    def test_person_works_the_exceptions_queue(self, client) -> None:
        client.post("/cash/transactions", json=[self.credit("U2", "20000", "TRANSFER", "X")])
        [txn] = client.get("/cash/transactions", params={"status": "unmatched"}).json()

        bad = client.post(
            f"/cash/transactions/{txn['id']}/apply",
            json={"allocations": [{"invoice_id": "INV-2", "amount": "99999"}]},
        )
        assert bad.status_code == 409

        ok = client.post(
            f"/cash/transactions/{txn['id']}/apply",
            json={"allocations": [{"invoice_id": "INV-2", "amount": "20000"}], "note": "ok"},
        )
        assert ok.json()["status"] == "applied"
        assert client.get("/dunning/invoices/INV-2").json()["state"]["state"] == "closed"
        again = client.post(f"/cash/transactions/{txn['id']}/dismiss", json={"note": "x"})
        assert again.status_code == 409

    def test_statement_upload(self, client) -> None:
        csv = (
            "value_date,amount,currency,reference,payer_name,utr\n"
            "2026-11-24,30000.00,INR,RTGS INV-3,CUSTOMER 3,S1\n"
        )
        resp = client.post("/cash/statement", content=csv, headers={"content-type": "text/csv"})
        assert resp.json()["matched"] == 1
        bad = client.post("/cash/statement", content="nope\n")
        assert bad.status_code == 422

    def test_simulate_endpoint(self, client) -> None:
        body = client.post("/cash/simulate", params={"count": 8, "seed": 7}).json()
        assert body["received"] == 8 and body["unmatched"] == 2

    def test_unknown_status_and_transaction(self, client) -> None:
        assert client.get("/cash/transactions", params={"status": "bogus"}).status_code == 422
        resp = client.post("/cash/transactions/nope/dismiss", json={"note": "x"})
        assert resp.status_code == 404
