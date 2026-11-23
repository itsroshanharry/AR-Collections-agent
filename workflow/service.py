"""The dunning workflow against the database.

The rules live in pure modules (policy, scheduler, state_machine). This module only
loads an invoice's row, hands its Snapshot to those functions, and writes back what they
return - the new state, the follow-up tasks, the audit event and the next scheduled
action - in one transaction.

Concurrency: the scheduler tick, the voice agent finishing a call and a person resolving
a task can all touch one invoice at the same moment. Rows carry a version column, so a
write based on a stale read raises instead of silently overwriting; every mutation here
re-reads and retries when that happens.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from ab_dashboard.db import (
    ContactAttemptRow,
    InvoiceEventRow,
    InvoiceStateRow,
    TaskRow,
    _new_id,
    get_sessionmaker,
)
from shared.models import CallOutcome, Invoice, RiskTier
from voice_agent.dunning_logic import classify_risk, priority_score
from workflow.policy import INTRUSIVE_CHANNELS, Channel, DunningPolicy, TaskType, get_policy
from workflow.scheduler import Attempt, NextAction, as_utc, next_action
from workflow.state_machine import (
    DunningState,
    Snapshot,
    TaskSpec,
    Transition,
    TransitionError,
    on_cadence_exhausted,
    on_call_outcome,
    on_contact,
    on_payment,
    on_promise_due,
    on_task_resolved,
    retier,
)

logger = logging.getLogger(__name__)

# Contact history older than this cannot affect any rule (the longest is 7 days).
ATTEMPT_LOOKBACK = timedelta(days=8)
MAX_RETRIES = 3


class NotFoundError(LookupError):
    pass


class ConcurrentUpdateError(RuntimeError):
    """Still conflicting after MAX_RETRIES re-reads. Rare; safe to retry the request."""


# --------------------------------------------------------------------------------------
# Row <-> domain
# --------------------------------------------------------------------------------------


def _snapshot(row: InvoiceStateRow) -> Snapshot:
    return Snapshot(
        invoice_id=row.invoice_id,
        state=DunningState(row.state),
        tier=RiskTier(row.tier),
        cadence_tier=RiskTier(row.cadence_tier),
        cadence_step=row.cadence_step,
        cadence_started_at=as_utc(row.cadence_started_at),
        last_contact_at=as_utc(row.last_contact_at) if row.last_contact_at else None,
        promise_to_pay_date=row.promise_to_pay_date,
        broken_promises=row.broken_promises,
        escalations=row.escalations,
        closed_reason=row.closed_reason,
    )


def _write_snapshot(row: InvoiceStateRow, snap: Snapshot) -> None:
    row.state = snap.state.value
    row.tier = snap.tier.value
    row.cadence_tier = snap.cadence_tier.value
    row.cadence_step = snap.cadence_step
    row.cadence_started_at = snap.cadence_started_at
    row.last_contact_at = snap.last_contact_at
    row.promise_to_pay_date = snap.promise_to_pay_date
    row.broken_promises = snap.broken_promises
    row.escalations = snap.escalations
    row.closed_reason = snap.closed_reason


def _tz(row: InvoiceStateRow, policy: DunningPolicy) -> ZoneInfo:
    return policy.timezone_for(row.timezone, row.invoice.get("currency", "INR"))


def local_today(row: InvoiceStateRow, policy: DunningPolicy, now: datetime) -> date:
    """Promise dates and SLAs are calendar dates in the customer's timezone."""
    return as_utc(now).astimezone(_tz(row, policy)).date()


def effective_invoice(row: InvoiceStateRow, today: date) -> Invoice:
    """The invoice as it stands today: aged since sync, net of payments received.

    This is what gets tiered, prioritised, and rendered into the call prompt - so the
    agent quotes the balance actually outstanding, and the judge grounds against it.
    """
    invoice = Invoice.model_validate(row.invoice)
    aged = row.snapshot_days_overdue + max(0, (today - row.synced_on).days)
    return invoice.model_copy(
        update={
            "days_overdue": aged,
            "amount": Decimal(row.amount_outstanding),
            "promise_to_pay_date": row.promise_to_pay_date,
        }
    )


def current_tier(row: InvoiceStateRow, today: date) -> RiskTier:
    return classify_risk(effective_invoice(row, today), broken_promises=row.broken_promises)


def scheduled_channel(row: InvoiceStateRow, policy: DunningPolicy) -> Channel:
    """The channel the cadence calls for at this invoice's current step."""
    cadence = policy.cadence(RiskTier(row.cadence_tier))
    channel = cadence[min(row.cadence_step, len(cadence) - 1)].channel
    if row.do_not_call and channel in INTRUSIVE_CHANNELS:
        return Channel.EMAIL
    return channel


async def _attempts(session: AsyncSession, customer_id: str, now: datetime) -> list[Attempt]:
    """Contact history for the *customer*, across all their invoices: three overdue
    invoices must not mean three times the calls."""
    rows = await session.execute(
        select(ContactAttemptRow.channel, ContactAttemptRow.attempted_at).where(
            ContactAttemptRow.customer_id == customer_id,
            ContactAttemptRow.attempted_at > as_utc(now) - ATTEMPT_LOOKBACK,
        )
    )
    return [Attempt(channel=Channel(c), at=as_utc(at)) for c, at in rows]


def _plan(
    row: InvoiceStateRow, attempts: list[Attempt], policy: DunningPolicy, now: datetime
) -> NextAction | None:
    snap = _snapshot(row)
    if not snap.automated:
        return None
    anchor = max(filter(None, [snap.last_contact_at, snap.cadence_started_at]))
    return next_action(
        cadence=policy.cadence(snap.cadence_tier),
        step_index=snap.cadence_step,
        anchor=anchor,
        attempts=attempts,
        rules=policy.rules,
        tz=_tz(row, policy),
        now=now,
        do_not_call=row.do_not_call,
    )


async def _reschedule(
    session: AsyncSession, row: InvoiceStateRow, policy: DunningPolicy, now: datetime
) -> NextAction | None:
    planned = _plan(row, await _attempts(session, row.customer_id, now), policy, now)
    row.next_action_channel = planned.channel.value if planned else None
    row.next_action_at = planned.at if planned else None
    return planned


# --------------------------------------------------------------------------------------
# Applying transitions
# --------------------------------------------------------------------------------------


async def _open_tasks(
    session: AsyncSession, row: InvoiceStateRow, specs: list[TaskSpec], call_log_id: str | None
) -> list[TaskRow]:
    if not specs:
        return []
    keys = [s.dedupe_key for s in specs]
    existing = set(
        (await session.execute(select(TaskRow.dedupe_key).where(TaskRow.dedupe_key.in_(keys))))
        .scalars()
        .all()
    )
    created = []
    for spec in specs:
        if spec.dedupe_key in existing:
            logger.info("Task %s already exists; not reopening", spec.dedupe_key)
            continue
        task = TaskRow(
            id=_new_id(),
            invoice_id=row.invoice_id,
            task_type=spec.task_type.value,
            title=spec.title,
            assignee=spec.assignee,
            status="open",
            due_date=spec.due_date,
            payload=spec.payload,
            dedupe_key=spec.dedupe_key,
            call_log_id=call_log_id,
        )
        session.add(task)
        created.append(task)
    return created


async def _close_tasks(
    session: AsyncSession,
    invoice_id: str,
    closes: list[tuple[TaskType, str]],
    now: datetime,
    *,
    except_id: str | None = None,
) -> None:
    for task_type, resolution in closes:
        stmt = (
            update(TaskRow)
            .where(
                TaskRow.invoice_id == invoice_id,
                TaskRow.task_type == task_type.value,
                TaskRow.status == "open",
            )
            .values(
                status="done",
                resolution=resolution,
                resolution_note="Closed automatically by the dunning workflow.",
                resolved_at=now,
            )
        )
        if except_id:
            stmt = stmt.where(TaskRow.id != except_id)
        await session.execute(stmt)


async def _apply(
    session: AsyncSession,
    row: InvoiceStateRow,
    transition: Transition,
    now: datetime,
    *,
    call_log_id: str | None = None,
    except_task_id: str | None = None,
) -> list[TaskRow]:
    from_state = row.state
    _write_snapshot(row, transition.snapshot)
    if transition.snapshot.closed_reason == "paid":
        # Settled by a confirmed payment rather than a recorded one (e.g. "received" on a
        # verify task): the ledger balance is zero either way.
        row.amount_outstanding = "0.00"
    created = await _open_tasks(session, row, transition.tasks, call_log_id)
    await _close_tasks(
        session, row.invoice_id, transition.close_tasks, now, except_id=except_task_id
    )
    session.add(
        InvoiceEventRow(
            invoice_id=row.invoice_id,
            event=transition.event,
            from_state=from_state,
            to_state=row.state,
            tier=row.tier,
            detail={**transition.detail, "tasks_opened": [t.task_type for t in created]},
            created_at=now,
        )
    )
    return created


def _log_event(row: InvoiceStateRow, event: str, from_state: str, now: datetime, **detail):
    return InvoiceEventRow(
        invoice_id=row.invoice_id,
        event=event,
        from_state=from_state,
        to_state=row.state,
        tier=row.tier,
        detail=detail,
        created_at=now,
    )


async def _retier(session: AsyncSession, row: InvoiceStateRow, now: datetime, today: date) -> bool:
    tier = current_tier(row, today)
    if tier.value == row.tier:
        return False
    before, from_state = row.tier, row.state
    _write_snapshot(row, retier(_snapshot(row), tier, now))
    session.add(_log_event(row, "retiered", from_state, now, **{"from": before, "to": tier.value}))
    return True


async def _with_retry[T](op: Callable[[AsyncSession], Awaitable[T]]) -> T:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with get_sessionmaker()() as session:
                result = await op(session)
                await session.commit()
                return result
        except StaleDataError:
            logger.info("Concurrent update; retrying (%d/%d)", attempt, MAX_RETRIES)
    raise ConcurrentUpdateError("invoice kept changing underneath this update; retry")


async def _load(session: AsyncSession, invoice_id: str) -> InvoiceStateRow:
    row = await session.get(InvoiceStateRow, invoice_id)
    if row is None:
        raise NotFoundError(f"No invoice {invoice_id!r} in the dunning workflow")
    return row


# --------------------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    created: int = 0
    already_present: int = 0
    disputed_on_arrival: int = 0


def _new_row(invoice: Invoice, now: datetime) -> InvoiceStateRow:
    tier = classify_risk(invoice)
    return InvoiceStateRow(
        invoice_id=invoice.invoice_id,
        customer_id=invoice.customer.customer_id,
        invoice=invoice.model_dump(mode="json"),
        amount_outstanding=f"{invoice.amount:.2f}",
        timezone=invoice.customer.timezone,
        do_not_call=False,
        state=DunningState.NEW.value,
        tier=tier.value,
        cadence_tier=tier.value,
        cadence_step=0,
        cadence_started_at=now,
        snapshot_days_overdue=invoice.days_overdue,
        synced_on=as_utc(now).date(),
        broken_promises=0,
        escalations=0,
    )


async def _add_invoice(
    session: AsyncSession, invoice: Invoice, now: datetime, policy: DunningPolicy
) -> InvoiceStateRow:
    row = _new_row(invoice, now)
    session.add(row)
    session.add(_log_event(row, "synced", "", now, tier=row.tier))
    if invoice.dispute_flag:
        # Already contested in the ledger: route to the disputes desk, not the dialler.
        today = local_today(row, policy, now)
        await _apply(
            session,
            row,
            Transition(
                snapshot=replace(_snapshot(row), state=DunningState.DISPUTED),
                event="disputed_on_sync",
                tasks=[
                    TaskSpec(
                        task_type=TaskType.REVIEW_DISPUTE,
                        title="Review invoice disputed in the ledger",
                        due_date=today + timedelta(days=policy.sla_days(TaskType.REVIEW_DISPUTE)),
                        dedupe_key=f"{invoice.invoice_id}:review_dispute:sync",
                        payload={"reason": "dispute flag set in ledger"},
                    )
                ],
            ),
            now,
        )
    await _reschedule(session, row, policy, now)
    return row


async def sync_invoices(
    invoices: list[Invoice], *, now: datetime | None = None, policy: DunningPolicy | None = None
) -> SyncReport:
    """Bring ledger invoices into the workflow. Idempotent: existing invoices keep their
    state; only new, overdue ones are added."""
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    report = SyncReport()
    async with get_sessionmaker()() as session:
        existing = set((await session.execute(select(InvoiceStateRow.invoice_id))).scalars())
        for invoice in invoices:
            if invoice.days_overdue <= 0:
                continue
            if invoice.invoice_id in existing:
                report.already_present += 1
                continue
            row = await _add_invoice(session, invoice, now, policy)
            existing.add(invoice.invoice_id)
            report.created += 1
            report.disputed_on_arrival += row.state == DunningState.DISPUTED.value
        await session.commit()
    return report


@dataclass
class TickReport:
    examined: int = 0
    emails_sent: int = 0
    sms_sent: int = 0
    calls_due: int = 0
    escalated: int = 0
    promises_broken: int = 0
    retiered: int = 0
    conflicts: int = 0


async def _tick_one(
    session: AsyncSession, invoice_id: str, report: TickReport, policy: DunningPolicy, now: datetime
) -> None:
    row = await _load(session, invoice_id)
    today = local_today(row, policy, now)

    broken = on_promise_due(_snapshot(row), policy, now, today)
    if broken:
        await _apply(session, row, broken, now)
        report.promises_broken += 1
        report.escalated += row.state == DunningState.ESCALATED.value

    if row.state != DunningState.CLOSED.value and await _retier(session, row, now, today):
        report.retiered += 1

    planned = await _reschedule(session, row, policy, now)
    if planned is None or planned.at > now:
        return

    if planned.channel is Channel.ESCALATE:
        await _apply(session, row, on_cadence_exhausted(_snapshot(row), policy, today), now)
        report.escalated += 1
    elif planned.channel is Channel.CALL:
        report.calls_due += 1  # left for the voice agent's queue
        return
    else:
        # Email and SMS are recorded as sent; delivery is outside this system's scope.
        session.add(
            ContactAttemptRow(
                invoice_id=row.invoice_id,
                customer_id=row.customer_id,
                channel=planned.channel.value,
                attempted_at=now,
                outcome="sent",
            )
        )
        await _apply(session, row, on_contact(_snapshot(row), planned.channel, now), now)
        if planned.channel is Channel.EMAIL:
            report.emails_sent += 1
        else:
            report.sms_sent += 1
        await session.flush()
    await _reschedule(session, row, policy, now)


async def tick(*, now: datetime | None = None, policy: DunningPolicy | None = None) -> TickReport:
    """One scheduler pass: lapse promises, re-tier aged invoices, send due emails/SMS,
    escalate exhausted cadences, and count the calls now due.

    Each invoice is its own transaction, so one conflict or failure never rolls back the
    rest of the pass. Safe to run from several workers: a concurrent edit to the same
    invoice makes one of them skip it until the next pass.
    """
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    report = TickReport()
    async with get_sessionmaker()() as session:
        ids = list(
            (
                await session.execute(
                    select(InvoiceStateRow.invoice_id).where(
                        InvoiceStateRow.state != DunningState.CLOSED.value
                    )
                )
            ).scalars()
        )
    for invoice_id in ids:
        report.examined += 1
        try:
            async with get_sessionmaker()() as session:
                await _tick_one(session, invoice_id, report, policy, now)
                await session.commit()
        except StaleDataError:
            report.conflicts += 1
        except Exception:  # noqa: BLE001 - one bad invoice must not stop the pass
            logger.exception("Tick failed for %s", invoice_id)
    return report


@dataclass
class DueCall:
    invoice: Invoice  # effective: aged and net of payments
    tier: RiskTier
    priority: float
    state: str
    broken_promises: int


async def due_calls(
    *, now: datetime | None = None, limit: int = 20, policy: DunningPolicy | None = None
) -> list[DueCall]:
    """Invoices whose cadence wants a call that is permitted right now, highest priority
    first. Re-checked against the contact rules at query time: a call that was due at
    18:55 is not due at 21:00."""
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(InvoiceStateRow).where(
                        InvoiceStateRow.state.in_(
                            [DunningState.NEW.value, DunningState.IN_DUNNING.value]
                        ),
                        InvoiceStateRow.next_action_channel == Channel.CALL.value,
                        InvoiceStateRow.next_action_at <= now,
                    )
                )
            ).scalars()
        )
        due: list[DueCall] = []
        for row in rows:
            planned = _plan(row, await _attempts(session, row.customer_id, now), policy, now)
            if planned is None or planned.channel is not Channel.CALL or planned.at > now:
                continue
            today = local_today(row, policy, now)
            invoice = effective_invoice(row, today)
            tier = RiskTier(row.tier)
            due.append(
                DueCall(
                    invoice=invoice.model_copy(update={"risk_tier": tier}),
                    tier=tier,
                    priority=priority_score(invoice),
                    state=row.state,
                    broken_promises=row.broken_promises,
                )
            )
    due.sort(key=lambda d: (-d.priority, d.invoice.invoice_id))
    return due[:limit]


def capture_discrepancy(captured: dict | None, outcome: CallOutcome) -> str | None:
    """Compare what the agent recorded live (tool calls) with what the transcript
    extractor concluded afterwards. They are independent signals; when they disagree,
    one of them is wrong and a person should look."""
    if not captured or not captured.get("outcome"):
        return None
    live = str(captured["outcome"])
    if live != outcome.outcome.value:
        return (
            f"agent recorded {live!r} during the call; transcript extraction says "
            f"{outcome.outcome.value!r}"
        )
    if live == "promise_to_pay":
        raw = str(captured.get("promise_to_pay_date") or "")
        try:
            live_date = date.fromisoformat(raw) if raw else None
        except ValueError:
            return f"agent recorded an unparseable promise date {raw!r}"
        if live_date != outcome.promise_to_pay_date:
            return (
                f"agent recorded promise date {live_date}; transcript extraction says "
                f"{outcome.promise_to_pay_date}"
            )
    return None


@dataclass
class OutcomeReport:
    invoice_id: str
    state: str
    tier: str
    tasks_opened: list[str] = field(default_factory=list)
    discrepancy: str | None = None
    next_action_channel: str | None = None
    next_action_at: datetime | None = None


async def record_call_outcome(
    invoice_id: str,
    outcome: CallOutcome,
    *,
    call_log_id: str | None = None,
    captured: dict | None = None,
    invoice: Invoice | None = None,
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> OutcomeReport:
    """Feed a finished call into the workflow.

    `captured` is what the agent's tools recorded during the call; it is reconciled
    against `outcome` (the post-call extraction) and any disagreement opens a QA task.
    `invoice` lets a call about an invoice not yet synced bring it into the workflow.
    """
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    attempt_id = _new_id()
    source_id = call_log_id or attempt_id

    async def op(session: AsyncSession) -> OutcomeReport:
        row = await session.get(InvoiceStateRow, invoice_id)
        if row is None:
            if invoice is None:
                raise NotFoundError(f"No invoice {invoice_id!r} in the dunning workflow")
            row = await _add_invoice(session, invoice, now, policy)
            await session.flush()
        today = local_today(row, policy, now)

        snap = _snapshot(row)
        transition = on_call_outcome(
            snap,
            outcome,
            call_id=source_id,
            policy=policy,
            now=now,
            today=today,
            current_step_channel=scheduled_channel(row, policy) if snap.automated else None,
            payment_reference=(captured or {}).get("payment_reference") or None,
        )
        discrepancy = capture_discrepancy(captured, outcome)
        if discrepancy:
            transition.detail["discrepancy"] = discrepancy
            transition.tasks.append(
                TaskSpec(
                    task_type=TaskType.REVIEW_CALL,
                    title="Live capture and transcript extraction disagree",
                    due_date=today + timedelta(days=policy.sla_days(TaskType.REVIEW_CALL)),
                    dedupe_key=f"{source_id}:{TaskType.REVIEW_CALL.value}",
                    payload={"discrepancy": discrepancy, "captured": captured or {}},
                )
            )

        session.add(
            ContactAttemptRow(
                id=attempt_id,
                invoice_id=row.invoice_id,
                customer_id=row.customer_id,
                channel=Channel.CALL.value,
                attempted_at=now,
                outcome=outcome.outcome.value,
                call_log_id=call_log_id,
            )
        )
        created = await _apply(session, row, transition, now, call_log_id=call_log_id)
        await session.flush()
        if row.state != DunningState.CLOSED.value:
            await _retier(session, row, now, today)
        await _reschedule(session, row, policy, now)
        return OutcomeReport(
            invoice_id=row.invoice_id,
            state=row.state,
            tier=row.tier,
            tasks_opened=[t.id for t in created],
            discrepancy=discrepancy,
            next_action_channel=row.next_action_channel,
            next_action_at=row.next_action_at,
        )

    return await _with_retry(op)


async def record_payment(
    invoice_id: str,
    amount: Decimal,
    *,
    reference: str = "",
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> InvoiceStateRow:
    """Apply a payment from the ledger. Stands in for an ERP/bank feed."""
    if amount <= 0:
        raise ValueError("payment amount must be positive")
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()

    async def op(session: AsyncSession) -> InvoiceStateRow:
        row = await _load(session, invoice_id)
        if row.state == DunningState.CLOSED.value:
            raise TransitionError(f"{invoice_id} is already closed ({row.closed_reason})")
        outstanding = max(Decimal(row.amount_outstanding) - amount, Decimal("0"))
        row.amount_outstanding = f"{outstanding:.2f}"
        await _apply(
            session,
            row,
            on_payment(_snapshot(row), outstanding, amount=amount, reference=reference),
            now,
        )
        if row.state != DunningState.CLOSED.value:
            await _retier(session, row, now, local_today(row, policy, now))
        await _reschedule(session, row, policy, now)
        return row

    return await _with_retry(op)


async def resolve_task(
    task_id: str,
    resolution: str,
    *,
    note: str = "",
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> tuple[TaskRow, InvoiceStateRow]:
    """A person closed a follow-up task. Its resolution drives the invoice onward."""
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()

    async def op(session: AsyncSession):
        task = await session.get(TaskRow, task_id)
        if task is None:
            raise NotFoundError(f"No task {task_id!r}")
        if task.status != "open":
            raise TransitionError(f"Task {task_id} is already {task.status}")
        row = await _load(session, task.invoice_id)
        today = local_today(row, policy, now)
        transition = on_task_resolved(
            _snapshot(row),
            TaskType(task.task_type),
            resolution,
            policy=policy,
            now=now,
            today=today,
        )
        task.status = "done"
        task.resolution = resolution
        task.resolution_note = note
        task.resolved_at = now
        transition.detail["task_id"] = task.id
        await _apply(session, row, transition, now, except_task_id=task.id)
        await session.flush()
        if row.state != DunningState.CLOSED.value:
            await _retier(session, row, now, today)
        await _reschedule(session, row, policy, now)
        return task, row

    return await _with_retry(op)


async def set_do_not_call(
    invoice_id: str, do_not_call: bool, *, now: datetime | None = None
) -> InvoiceStateRow:
    now = as_utc(now or datetime.now(UTC))
    policy = get_policy()

    async def op(session: AsyncSession) -> InvoiceStateRow:
        row = await _load(session, invoice_id)
        row.do_not_call = do_not_call
        session.add(_log_event(row, "do_not_call_set", row.state, now, do_not_call=do_not_call))
        await _reschedule(session, row, policy, now)
        return row

    return await _with_retry(op)


@dataclass
class InvoiceDetail:
    row: InvoiceStateRow
    invoice: Invoice
    tasks: list[TaskRow]
    events: list[InvoiceEventRow]
    attempts: list[ContactAttemptRow]


async def invoice_detail(invoice_id: str, *, now: datetime | None = None) -> InvoiceDetail:
    now = as_utc(now or datetime.now(UTC))
    policy = get_policy()
    async with get_sessionmaker()() as session:
        row = await _load(session, invoice_id)
        tasks = list(
            (
                await session.execute(
                    select(TaskRow)
                    .where(TaskRow.invoice_id == invoice_id)
                    .order_by(TaskRow.created_at)
                )
            ).scalars()
        )
        events = list(
            (
                await session.execute(
                    select(InvoiceEventRow)
                    .where(InvoiceEventRow.invoice_id == invoice_id)
                    .order_by(InvoiceEventRow.created_at, InvoiceEventRow.id)
                )
            ).scalars()
        )
        attempts = list(
            (
                await session.execute(
                    select(ContactAttemptRow)
                    .where(ContactAttemptRow.invoice_id == invoice_id)
                    .order_by(ContactAttemptRow.attempted_at)
                )
            ).scalars()
        )
    return InvoiceDetail(
        row=row,
        invoice=effective_invoice(row, local_today(row, policy, now)),
        tasks=tasks,
        events=events,
        attempts=attempts,
    )


async def has_invoices() -> bool:
    async with get_sessionmaker()() as session:
        return (
            await session.scalar(select(InvoiceStateRow.invoice_id).limit(1))
        ) is not None


async def list_tasks(
    *,
    assignee: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    invoice_id: str | None = None,
    limit: int = 100,
) -> list[TaskRow]:
    stmt = select(TaskRow).order_by(TaskRow.due_date, TaskRow.created_at).limit(limit)
    if assignee:
        stmt = stmt.where(TaskRow.assignee == assignee)
    if status:
        stmt = stmt.where(TaskRow.status == status)
    if task_type:
        stmt = stmt.where(TaskRow.task_type == task_type)
    if invoice_id:
        stmt = stmt.where(TaskRow.invoice_id == invoice_id)
    async with get_sessionmaker()() as session:
        return list((await session.execute(stmt)).scalars())
