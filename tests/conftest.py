from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

from shared.models import Customer, Invoice, JudgeDimension, JudgeScore


@pytest.fixture
def invoice() -> Invoice:
    return Invoice(
        invoice_id="INV-10432",
        customer=Customer(
            customer_id="CUST-1",
            name="Acme Pvt Ltd",
            contact_name="Priya Sharma",
            gstin="29ABCDE1234F1Z5",
        ),
        amount=Decimal("45000.00"),
        currency="INR",
        invoice_date=date(2026, 5, 1),
        due_date=date(2026, 6, 1),
        days_overdue=62,
        hsn_sac_code="998314",
        dispute_flag=False,
        reminder_count=2,
    )


def make_invoice(**overrides) -> Invoice:
    base = {
        "invoice_id": "INV-1",
        "customer": Customer(customer_id="C-1", name="Test Co"),
        "amount": Decimal("50000.00"),
        "currency": "INR",
        "invoice_date": date(2026, 5, 1),
        "due_date": date(2026, 6, 1),
        "days_overdue": 30,
        "dispute_flag": False,
        "reminder_count": 0,
    }
    base.update(overrides)
    return Invoice(**base)


def perfect_score() -> JudgeScore:
    return JudgeScore(scores=dict.fromkeys(JudgeDimension, 5))


@pytest.fixture
def database_url(tmp_path) -> str:
    """A fresh, empty database for one test.

    SQLite by default. With TEST_POSTGRES_URL set (as CI does), the same test runs against
    real Postgres instead: the public schema is dropped and recreated first, so every test
    starts from nothing exactly as it does on SQLite. Running the whole suite on both is
    what catches a dialect difference before it reaches the deployed database.
    """
    postgres = os.getenv("TEST_POSTGRES_URL")
    if not postgres:
        return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    import psycopg

    plain = postgres.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgres://", "postgresql://"
    )
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return postgres
