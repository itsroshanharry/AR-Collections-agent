"""Cash application against the database: bank lines in, invoices settled.

The matching rules are pure (workflow/cash_application.py). This module loads the open
invoices, asks the matcher what a line pays, records the decision, and applies the money
through the same payment path a hand-posted payment uses - so a matched payment closes
the invoice and its open follow-ups exactly as before, with the bank line in the audit
trail.

Each line is its own transaction: a later line is matched against balances that already
reflect the earlier ones, and one bad line never rolls back the rest of a statement.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ab_dashboard.db import BankTransactionRow, InvoiceStateRow, TaskRow, get_sessionmaker
from shared.observability import metrics as m
from shared.observability.tracing import tracer
from workflow import service
from workflow.cash_application import BankLine, MatchResult, OpenItem, match
from workflow.policy import DunningPolicy, TaskType, get_policy
from workflow.scheduler import as_utc
from workflow.state_machine import DunningState, TransitionError

logger = logging.getLogger(__name__)

SOURCES = frozenset({"feed", "statement", "simulated"})


class StatementError(ValueError):
    """A statement file that cannot be read. Carries the line number at fault."""


@dataclass
class LineResult:
    utr: str
    status: str  # matched | unmatched | duplicate
    rule: str
    reason: str
    amount: str
    currency: str
    customer_id: str | None = None
    allocations: list[dict] = field(default_factory=list)
    transaction_id: str | None = None


@dataclass
class IngestReport:
    received: int = 0
    matched: int = 0
    unmatched: int = 0
    duplicates: int = 0
    invoices_settled: int = 0
    invoices_part_paid: int = 0
    lines: list[LineResult] = field(default_factory=list)

    @property
    def auto_match_rate(self) -> float | None:
        decided = self.matched + self.unmatched
        return round(self.matched / decided, 3) if decided else None


def _item(row: InvoiceStateRow) -> OpenItem:
    inv = row.invoice
    return OpenItem(
        invoice_id=row.invoice_id,
        customer_id=row.customer_id,
        customer_name=inv.get("customer", {}).get("name", ""),
        currency=inv.get("currency", "INR").upper(),
        outstanding=Decimal(row.amount_outstanding),
        due_date=date.fromisoformat(inv["due_date"]),
    )


async def _claims(session: AsyncSession) -> dict[str, str]:
    """Payment references customers quoted on calls, from the open reconcile tasks."""
    tasks = (
        await session.execute(
            select(TaskRow.invoice_id, TaskRow.payload).where(
                TaskRow.task_type == TaskType.RECONCILE_PAYMENT.value, TaskRow.status == "open"
            )
        )
    ).all()
    return {
        str(payload.get("payment_reference")): invoice_id
        for invoice_id, payload in tasks
        if payload and payload.get("payment_reference")
    }


async def _apply_allocations(
    session: AsyncSession,
    txn: BankTransactionRow,
    result_allocations,
    rule: str,
    report: IngestReport | None,
    *,
    now: datetime,
    policy: DunningPolicy,
) -> list[dict]:
    applied = []
    for alloc in result_allocations:
        row = await service._load(session, alloc.invoice_id)
        await service.apply_payment(
            session,
            row,
            alloc.amount,
            reference=txn.utr,
            now=now,
            policy=policy,
            settles=alloc.settles,
            detail={"bank_transaction": txn.id, "match_rule": rule},
        )
        closed = row.state == DunningState.CLOSED.value
        if report is not None:
            report.invoices_settled += closed
            report.invoices_part_paid += not closed
        applied.append(
            {"invoice_id": alloc.invoice_id, "amount": f"{alloc.amount:.2f}", "settles": closed}
        )
    return applied


async def _ingest_one(
    session: AsyncSession,
    line: BankLine,
    source: str,
    report: IngestReport,
    *,
    now: datetime,
    policy: DunningPolicy,
) -> LineResult:
    base = {"utr": line.utr, "amount": f"{line.amount:.2f}", "currency": line.currency.upper()}
    seen = select(BankTransactionRow.id).where(BankTransactionRow.utr == line.utr)
    if await session.scalar(seen):
        return LineResult(status="duplicate", rule="none", reason="already received", **base)

    rows = list(
        (
            await session.execute(
                select(InvoiceStateRow).where(InvoiceStateRow.state != DunningState.CLOSED.value)
            )
        ).scalars()
    )
    closed = frozenset(
        (
            await session.execute(
                select(InvoiceStateRow.invoice_id).where(
                    InvoiceStateRow.state == DunningState.CLOSED.value
                )
            )
        ).scalars()
    )
    result: MatchResult = match(
        line,
        [_item(r) for r in rows],
        tolerance=policy.tolerance(line.currency),
        closed_invoice_ids=closed,
        claims=await _claims(session),
    )
    txn = BankTransactionRow(
        id=service._new_id(),
        utr=line.utr,
        value_date=line.value_date,
        amount=f"{line.amount:.2f}",
        currency=line.currency.upper(),
        reference=line.reference,
        payer_name=line.payer_name,
        source=source,
        status="matched" if result.matched else "unmatched",
        match_rule=result.rule,
        reason=result.reason,
        customer_id=result.customer_id,
        allocations=[],
        received_at=now,
    )
    session.add(txn)
    await session.flush()
    if result.matched:
        txn.allocations = await _apply_allocations(
            session, txn, result.allocations, result.rule, report, now=now, policy=policy
        )
    status, rule = txn.status, txn.match_rule
    service._on_commit(session, lambda: m.CASH_TRANSACTIONS.labels(status, rule).inc())
    return LineResult(
        status=txn.status,
        rule=txn.match_rule,
        reason=txn.reason,
        customer_id=txn.customer_id,
        allocations=txn.allocations,
        transaction_id=txn.id,
        **base,
    )


async def ingest(
    lines: list[BankLine],
    *,
    source: str = "feed",
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> IngestReport:
    """Receive bank credits: match each to open invoices and apply it, or queue it as an
    exception. Idempotent on the UTR, so a feed can be replayed safely."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    report = IngestReport()
    with tracer.start_as_current_span("cash.ingest") as span:
        for line in lines:
            report.received += 1
            try:
                result = await _ingest_line(line, source, report, now=now, policy=policy)
            except IntegrityError:
                # The same UTR arriving concurrently from another worker.
                result = LineResult(
                    utr=line.utr,
                    status="duplicate",
                    rule="none",
                    reason="already received",
                    amount=f"{line.amount:.2f}",
                    currency=line.currency.upper(),
                )
            report.lines.append(result)
            if result.status == "duplicate":
                report.duplicates += 1
            elif result.status == "matched":
                report.matched += 1
            else:
                report.unmatched += 1
        for key in ("received", "matched", "unmatched", "duplicates", "invoices_settled"):
            span.set_attribute(f"cash.{key}", getattr(report, key))
    return report


async def _ingest_line(line, source, report, *, now, policy) -> LineResult:
    # Counters on the report are only bumped by a transaction that commits; a retry
    # after a version conflict starts the line's tallies again.
    async def op(session: AsyncSession) -> tuple[LineResult, IngestReport]:
        scratch = IngestReport()
        return await _ingest_one(session, line, source, scratch, now=now, policy=policy), scratch

    result, scratch = await service._with_retry(op)
    report.invoices_settled += scratch.invoices_settled
    report.invoices_part_paid += scratch.invoices_part_paid
    return result


# --------------------------------------------------------------------------------------
# The exceptions queue
# --------------------------------------------------------------------------------------


async def _load_txn(session: AsyncSession, txn_id: str) -> BankTransactionRow:
    txn = await session.get(BankTransactionRow, txn_id)
    if txn is None:
        raise service.NotFoundError(f"No bank transaction {txn_id!r}")
    if txn.status != "unmatched":
        raise TransitionError(f"Transaction {txn_id} is already {txn.status}")
    return txn


async def apply_manually(
    txn_id: str,
    allocations: list[tuple[str, Decimal]],
    *,
    note: str = "",
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> BankTransactionRow:
    """A person decided where an unmatched payment goes. Any part of it not allocated
    (an overpayment to refund) is recorded on the transaction, not on an invoice."""
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    if not allocations:
        raise ValueError("allocate the payment to at least one invoice")
    if any(amount <= 0 for _, amount in allocations):
        raise ValueError("allocation amounts must be positive")
    if len({i for i, _ in allocations}) != len(allocations):
        raise ValueError("each invoice may appear once")

    async def op(session: AsyncSession) -> BankTransactionRow:
        txn = await _load_txn(session, txn_id)
        total = sum((a for _, a in allocations), Decimal("0"))
        if total > Decimal(txn.amount):
            raise ValueError(f"allocations total {total:.2f}, more than the {txn.amount} received")
        planned = []
        for invoice_id, amount in allocations:
            row = await service._load(session, invoice_id)
            item = _item(row)
            if item.currency != txn.currency:
                raise ValueError(f"{invoice_id} is billed in {item.currency}, not {txn.currency}")
            if amount > item.outstanding:
                raise ValueError(
                    f"{amount:.2f} is more than the {item.outstanding:.2f} owed on {invoice_id}"
                )
            settles = item.outstanding - amount <= policy.tolerance(txn.currency)
            planned.append(_Alloc(invoice_id, amount, settles))
        txn.allocations = await _apply_allocations(
            session, txn, planned, "manual", None, now=now, policy=policy
        )
        txn.status = "applied"
        unapplied = Decimal(txn.amount) - total
        txn.resolution_note = note + (f" (unapplied: {unapplied:.2f})" if unapplied else "")
        txn.resolved_at = now
        service._on_commit(session, lambda: m.CASH_TRANSACTIONS.labels("applied", "manual").inc())
        return txn

    return await service._with_retry(op)


@dataclass(frozen=True)
class _Alloc:
    invoice_id: str
    amount: Decimal
    settles: bool


async def dismiss(txn_id: str, *, note: str, now: datetime | None = None) -> BankTransactionRow:
    """Not a customer payment for any invoice here (a transfer between own accounts, a
    refund to send back). Kept, with the reason, so the statement still reconciles."""
    if not note.strip():
        raise ValueError("say why the transaction is dismissed")
    now = as_utc(now or datetime.now(UTC))

    async def op(session: AsyncSession) -> BankTransactionRow:
        txn = await _load_txn(session, txn_id)
        txn.status = "dismissed"
        txn.resolution_note = note
        txn.resolved_at = now
        service._on_commit(
            session, lambda: m.CASH_TRANSACTIONS.labels("dismissed", "manual").inc()
        )
        return txn

    return await service._with_retry(op)


async def list_transactions(
    *, status: str | None = None, customer_id: str | None = None, limit: int = 100
) -> list[BankTransactionRow]:
    stmt = (
        select(BankTransactionRow)
        .order_by(BankTransactionRow.received_at.desc(), BankTransactionRow.utr)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(BankTransactionRow.status == status)
    if customer_id:
        stmt = stmt.where(BankTransactionRow.customer_id == customer_id)
    async with get_sessionmaker()() as session:
        return list((await session.execute(stmt)).scalars())


# --------------------------------------------------------------------------------------
# "Already paid" on a call
# --------------------------------------------------------------------------------------


async def verify_reported_payment(
    session: AsyncSession,
    row: InvoiceStateRow,
    reference: str | None,
    *,
    now: datetime,
    policy: DunningPolicy,
) -> str | None:
    """The customer just said on a call that this invoice is paid. If that payment is
    already sitting in the exceptions queue (it arrived without an invoice reference),
    find it by the reference they quoted and apply it now. Returns the transaction id,
    or None, leaving the reconcile task for the feed or a person to settle."""
    if not reference:
        return None
    candidates = list(
        (
            await session.execute(
                select(BankTransactionRow).where(BankTransactionRow.status == "unmatched")
            )
        ).scalars()
    )
    claims = {reference: row.invoice_id}
    for txn in candidates:
        result = match(
            _line(txn), [_item(row)], tolerance=policy.tolerance(txn.currency), claims=claims
        )
        if result.matched and result.rule == "customer_claim":
            txn.status = "matched"
            txn.match_rule = result.rule
            txn.reason = result.reason
            txn.customer_id = row.customer_id
            txn.allocations = await _apply_allocations(
                session, txn, result.allocations, result.rule, None, now=now, policy=policy
            )
            service._on_commit(
                session, lambda: m.CASH_TRANSACTIONS.labels("matched", "customer_claim").inc()
            )
            return txn.id
    return None


def _line(txn: BankTransactionRow) -> BankLine:
    return BankLine(
        utr=txn.utr,
        value_date=txn.value_date,
        amount=Decimal(txn.amount),
        currency=txn.currency,
        reference=txn.reference,
        payer_name=txn.payer_name,
    )


# --------------------------------------------------------------------------------------
# Statement files
# --------------------------------------------------------------------------------------

STATEMENT_COLUMNS = ("value_date", "amount", "currency", "reference", "payer_name", "utr")


def parse_statement(text: str) -> list[BankLine]:
    """A bank statement export as CSV, credits only. Columns: value_date (YYYY-MM-DD),
    amount, currency, reference, payer_name, utr. Debits (amount <= 0) are skipped:
    cash application only concerns money coming in."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in STATEMENT_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise StatementError(f"statement is missing columns: {', '.join(missing)}")
    lines = []
    for n, raw in enumerate(reader, start=2):
        try:
            amount = Decimal(raw["amount"].replace(",", "").strip())
            value_date = date.fromisoformat(raw["value_date"].strip())
        except (InvalidOperation, ValueError) as exc:
            raise StatementError(f"line {n}: {exc}") from exc
        utr = (raw["utr"] or "").strip()
        if not utr:
            raise StatementError(f"line {n}: every line needs a utr")
        if amount <= 0:
            continue
        lines.append(
            BankLine(
                utr=utr,
                value_date=value_date,
                amount=amount,
                currency=(raw["currency"] or "INR").strip().upper(),
                reference=(raw["reference"] or "").strip(),
                payer_name=(raw["payer_name"] or "").strip(),
            )
        )
    return lines


async def simulate(
    *, count: int = 8, seed: int | None = None, now: datetime | None = None
) -> IngestReport:
    """Generate a day of realistic credits from the open invoices and ingest them: the
    demo stand-in for a bank feed (see workflow/bank_feed.py)."""
    from workflow.bank_feed import simulate_feed

    now = as_utc(now or datetime.now(UTC))
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(InvoiceStateRow).where(InvoiceStateRow.state != DunningState.CLOSED.value)
            )
        ).scalars()
        items = [_item(r) for r in rows]
    lines = simulate_feed(items, today=now.date(), count=count, seed=seed)
    return await ingest(lines, source="simulated", now=now)
