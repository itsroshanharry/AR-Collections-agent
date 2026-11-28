"""Cash application API: receive bank credits, see what matched, work the exceptions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ab_dashboard.auth import OPERATOR
from ab_dashboard.db import BankTransactionRow
from ab_dashboard.routers.dunning import _raise_for
from workflow import cash
from workflow.cash_application import BankLine

router = APIRouter(prefix="/cash", tags=["cash application"])

STATUSES = ("matched", "unmatched", "applied", "dismissed")


class BankLineIn(BaseModel):
    utr: str = Field(min_length=1, max_length=64, examples=["N24781190042"])
    value_date: date
    amount: Decimal = Field(gt=0)
    currency: str = "INR"
    reference: str = Field(default="", examples=["NEFT/INV-100432/ACME"])
    payer_name: str = ""

    def to_line(self) -> BankLine:
        return BankLine(
            utr=self.utr.strip(),
            value_date=self.value_date,
            amount=self.amount,
            currency=self.currency.upper(),
            reference=self.reference,
            payer_name=self.payer_name,
        )


class LineResultOut(BaseModel):
    utr: str
    status: str
    rule: str
    reason: str
    amount: str
    currency: str
    customer_id: str | None
    allocations: list[dict]
    transaction_id: str | None


class IngestOut(BaseModel):
    received: int
    matched: int
    unmatched: int
    duplicates: int
    invoices_settled: int
    invoices_part_paid: int
    auto_match_rate: float | None
    lines: list[LineResultOut]


class TransactionOut(BaseModel):
    id: str
    utr: str
    value_date: date
    amount: str
    currency: str
    reference: str
    payer_name: str
    source: str
    status: str
    match_rule: str
    reason: str
    customer_id: str | None
    allocations: list[dict]
    resolution_note: str
    resolved_by: str | None
    received_at: datetime
    resolved_at: datetime | None


class AllocationIn(BaseModel):
    invoice_id: str
    amount: Decimal = Field(gt=0)


class ApplyIn(BaseModel):
    allocations: list[AllocationIn] = Field(min_length=1)
    note: str = ""


class DismissIn(BaseModel):
    note: str = Field(min_length=1, examples=["transfer from our own savings account"])


def _ingest_out(report: cash.IngestReport) -> IngestOut:
    return IngestOut(
        received=report.received,
        matched=report.matched,
        unmatched=report.unmatched,
        duplicates=report.duplicates,
        invoices_settled=report.invoices_settled,
        invoices_part_paid=report.invoices_part_paid,
        auto_match_rate=report.auto_match_rate,
        lines=[LineResultOut(**line.__dict__) for line in report.lines],
    )


def _txn_out(txn: BankTransactionRow) -> TransactionOut:
    return TransactionOut(
        id=txn.id,
        utr=txn.utr,
        value_date=txn.value_date,
        amount=txn.amount,
        currency=txn.currency,
        reference=txn.reference,
        payer_name=txn.payer_name,
        source=txn.source,
        status=txn.status,
        match_rule=txn.match_rule,
        reason=txn.reason,
        customer_id=txn.customer_id,
        allocations=txn.allocations or [],
        resolution_note=txn.resolution_note,
        resolved_by=txn.resolved_by,
        received_at=txn.received_at,
        resolved_at=txn.resolved_at,
    )


@router.post("/transactions", response_model=IngestOut, dependencies=[OPERATOR])
async def receive(lines: list[BankLineIn]) -> IngestOut:
    """Receive credits from a bank feed. Each is matched and applied, or queued as an
    exception. Idempotent on utr: replaying the feed applies nothing twice."""
    return _ingest_out(await cash.ingest([line.to_line() for line in lines], source="feed"))


@router.post(
    "/statement",
    response_model=IngestOut,
    dependencies=[OPERATOR],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "text/csv": {
                    "schema": {"type": "string"},
                    "example": "value_date,amount,currency,reference,payer_name,utr\n"
                    "2026-11-24,50000.00,INR,NEFT/INV-100432/ACME,ACME PVT LTD,N2478119\n",
                }
            },
        }
    },
)
async def upload_statement(request: Request) -> IngestOut:
    """Import a bank statement CSV (body: text/csv). Columns: value_date, amount,
    currency, reference, payer_name, utr. Debits are skipped."""
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    try:
        lines = cash.parse_statement(text)
    except cash.StatementError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _ingest_out(await cash.ingest(lines, source="statement"))


@router.post("/simulate", response_model=IngestOut, dependencies=[OPERATOR])
async def simulate(
    count: int = Query(default=8, ge=1, le=100), seed: int | None = None
) -> IngestOut:
    """Generate and ingest a day of realistic credits from the open invoices: clean
    payments, a part payment, bank charges, an account payment, and a few that no rule
    can match. The demo stand-in for a real bank feed."""
    return _ingest_out(await cash.simulate(count=count, seed=seed))


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    status_: str | None = Query(default=None, alias="status", examples=["unmatched"]),
    customer_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TransactionOut]:
    """Received credits, newest first. status=unmatched is the exceptions queue."""
    if status_ and status_ not in STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status must be one of {', '.join(STATUSES)}"
        )
    rows = await cash.list_transactions(status=status_, customer_id=customer_id, limit=limit)
    return [_txn_out(t) for t in rows]


@router.post("/transactions/{txn_id}/apply", response_model=TransactionOut, dependencies=[OPERATOR])
async def apply(txn_id: str, payload: ApplyIn) -> TransactionOut:
    """Apply an unmatched credit to the invoices a person decided it pays."""
    try:
        txn = await cash.apply_manually(
            txn_id, [(a.invoice_id, a.amount) for a in payload.allocations], note=payload.note
        )
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return _txn_out(txn)


@router.post(
    "/transactions/{txn_id}/dismiss", response_model=TransactionOut, dependencies=[OPERATOR]
)
async def dismiss(txn_id: str, payload: DismissIn) -> TransactionOut:
    """Mark an unmatched credit as not a customer payment, with the reason."""
    try:
        txn = await cash.dismiss(txn_id, note=payload.note)
    except Exception as exc:  # noqa: BLE001
        _raise_for(exc)
    return _txn_out(txn)
