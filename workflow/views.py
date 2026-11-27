"""Read models for the operator console: the overview figures and the customer list.

Read-only queries over the workflow and cash tables, shaped for a screen rather than for
Prometheus. Nothing here changes state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from ab_dashboard.db import BankTransactionRow, InvoiceStateRow, TaskRow, get_sessionmaker
from shared.models import Invoice, RiskTier
from voice_agent.dunning_logic import amount_in_inr
from workflow import service
from workflow.policy import get_policy
from workflow.scheduler import as_utc
from workflow.state_machine import AUTOMATED_STATES, DunningState

_TIER_ORDER = [t.value for t in RiskTier]


@dataclass
class Summary:
    invoices: int
    invoices_by_state: dict[str, int]
    invoices_by_tier: dict[str, int]  # open invoices only
    # INR-normalised, open invoices only, so tiers in different currencies compare.
    outstanding_inr_by_tier: dict[str, Decimal]
    customers_with_calls_due: int
    open_tasks_by_team: dict[str, int]
    overdue_tasks_by_team: dict[str, int]
    bank_transactions_by_status: dict[str, int]
    # Of the credits cash application has decided, the share matched with no person.
    auto_match_rate: float | None


async def summary(*, now: datetime | None = None) -> Summary:
    now = as_utc(now or datetime.now(UTC))
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(
                    InvoiceStateRow.state,
                    InvoiceStateRow.tier,
                    InvoiceStateRow.amount_outstanding,
                    InvoiceStateRow.invoice,
                )
            )
        ).all()
        open_tasks = (
            await session.execute(
                select(TaskRow.assignee, func.count())
                .where(TaskRow.status == "open")
                .group_by(TaskRow.assignee)
            )
        ).all()
        overdue = (
            await session.execute(
                select(TaskRow.assignee, func.count())
                .where(TaskRow.status == "open", TaskRow.due_date < now.date())
                .group_by(TaskRow.assignee)
            )
        ).all()
        txns = (
            await session.execute(
                select(BankTransactionRow.status, func.count()).group_by(
                    BankTransactionRow.status
                )
            )
        ).all()

    by_state: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)
    outstanding: dict[str, Decimal] = defaultdict(Decimal)
    for state, tier, amount, invoice in rows:
        by_state[state] += 1
        if state == DunningState.CLOSED.value:
            continue
        by_tier[tier] += 1
        stub = Invoice.model_validate(invoice).model_copy(update={"amount": Decimal(amount)})
        outstanding[tier] += amount_in_inr(stub)

    by_status = {status: count for status, count in txns}
    # Applied and dismissed credits were unmatched first: a person had to decide them.
    decided = sum(by_status.values())
    rate = round(by_status.get("matched", 0) / decided, 3) if decided else None
    calls = await service.due_calls(now=now, limit=100_000, with_account=False)

    return Summary(
        invoices=len(rows),
        invoices_by_state=dict(by_state),
        invoices_by_tier={t: by_tier[t] for t in _TIER_ORDER if t in by_tier},
        outstanding_inr_by_tier={
            t: outstanding[t].quantize(Decimal("0.01")) for t in _TIER_ORDER if t in outstanding
        },
        customers_with_calls_due=len(calls),
        open_tasks_by_team=dict(open_tasks),
        overdue_tasks_by_team=dict(overdue),
        bank_transactions_by_status=by_status,
        auto_match_rate=rate,
    )


@dataclass
class CustomerRow:
    customer_id: str
    customer_name: str
    invoices: int
    open_invoices: int
    # The worst risk tier among open invoices; None when everything is closed.
    tier: str | None
    overdue_in_dunning: dict[str, Decimal] = field(default_factory=dict)
    outstanding: dict[str, Decimal] = field(default_factory=dict)
    outstanding_inr: Decimal = Decimal("0")
    open_tasks: int = 0
    states: dict[str, int] = field(default_factory=dict)


@dataclass
class CustomerPage:
    total: int
    customers: list[CustomerRow]


SORTS = ("outstanding", "name", "tier")


async def list_customers(
    *,
    q: str | None = None,
    tier: str | None = None,
    has_open_tasks: bool | None = None,
    sort: str = "outstanding",
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> CustomerPage:
    """Every customer in the workflow with their balances, worst open tier and open task
    count. Filtered and sorted in Python: amounts are stored as text (no decimal type on
    SQLite), and the ledger is hundreds of rows, not millions."""
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {', '.join(SORTS)}")
    now = as_utc(now or datetime.now(UTC))
    policy = get_policy()
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(InvoiceStateRow))).scalars())
        task_counts = dict(
            (
                await session.execute(
                    select(InvoiceStateRow.customer_id, func.count())
                    .join(TaskRow, TaskRow.invoice_id == InvoiceStateRow.invoice_id)
                    .where(TaskRow.status == "open")
                    .group_by(InvoiceStateRow.customer_id)
                )
            ).all()
        )

    customers: dict[str, CustomerRow] = {}
    for row in rows:
        c = customers.get(row.customer_id)
        if c is None:
            c = customers[row.customer_id] = CustomerRow(
                customer_id=row.customer_id,
                customer_name=row.invoice.get("customer", {}).get("name", ""),
                invoices=0,
                open_invoices=0,
                tier=None,
                open_tasks=task_counts.get(row.customer_id, 0),
            )
        c.invoices += 1
        c.states[row.state] = c.states.get(row.state, 0) + 1
        if row.state == DunningState.CLOSED.value:
            continue
        c.open_invoices += 1
        if c.tier is None or _TIER_ORDER.index(row.tier) > _TIER_ORDER.index(c.tier):
            c.tier = row.tier
        inv = service.effective_invoice(row, service.local_today(row, policy, now))
        c.outstanding[inv.currency] = c.outstanding.get(inv.currency, Decimal("0")) + inv.amount
        c.outstanding_inr += amount_in_inr(inv)
        if DunningState(row.state) in AUTOMATED_STATES:
            c.overdue_in_dunning[inv.currency] = (
                c.overdue_in_dunning.get(inv.currency, Decimal("0")) + inv.amount
            )

    result = list(customers.values())
    if q:
        needle = q.casefold()
        result = [
            c
            for c in result
            if needle in c.customer_name.casefold() or needle in c.customer_id.casefold()
        ]
    if tier:
        result = [c for c in result if c.tier == tier]
    if has_open_tasks is not None:
        result = [c for c in result if (c.open_tasks > 0) == has_open_tasks]

    if sort == "name":
        result.sort(key=lambda c: (c.customer_name.casefold(), c.customer_id))
    elif sort == "tier":
        result.sort(
            key=lambda c: (-(_TIER_ORDER.index(c.tier) if c.tier else -1), -c.outstanding_inr)
        )
    else:
        result.sort(key=lambda c: (-c.outstanding_inr, c.customer_id))
    return CustomerPage(total=len(result), customers=result[offset : offset + limit])
