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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from ab_dashboard.db import (
    BankTransactionRow,
    ContactAttemptRow,
    InvoiceEventRow,
    InvoiceStateRow,
    TaskRow,
    _new_id,
    get_sessionmaker,
)
from shared.models import AccountItem, CallOutcome, Invoice, RiskTier
from shared.observability import metrics as m
from shared.observability.tracing import tracer
from voice_agent.dunning_logic import amount_in_inr, classify_risk, priority_score
from workflow import audit
from workflow.policy import INTRUSIVE_CHANNELS, Channel, DunningPolicy, TaskType, get_policy
from workflow.scheduler import Attempt, NextAction, as_utc, next_action
from workflow.state_machine import (
    AUTOMATED_STATES,
    DunningState,
    Snapshot,
    TaskSpec,
    Transition,
    TransitionError,
    on_account_contact,
    on_cadence_exhausted,
    on_call_outcome,
    on_contact,
    on_payment,
    on_promise_due,
    on_task_resolved,
    promise_covers_account,
    retier,
)

logger = logging.getLogger(__name__)

# Contact history older than this cannot affect any rule (the longest is 7 days).
ATTEMPT_LOOKBACK = timedelta(days=8)
MAX_RETRIES = 3


def _on_commit(session: AsyncSession, record) -> None:
    """Queue a metric update to run only if this transaction commits. Counting at the
    point of change would double-count every write that loses a version conflict and is
    retried."""
    session.info.setdefault("metrics", []).append(record)


def _flush_metrics(session: AsyncSession) -> None:
    for record in session.info.pop("metrics", []):
        record()


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


async def _account_rows(
    session: AsyncSession,
    customer_id: str,
    *,
    exclude: str,
    only: list[str] | None = None,
) -> list[InvoiceStateRow]:
    """The customer's other invoices in active dunning, in a stable order. These are
    the ones an account-level contact covers; disputed, promised or escalated invoices
    are each waiting on something specific and are left out of the conversation."""
    stmt = (
        select(InvoiceStateRow)
        .where(
            InvoiceStateRow.customer_id == customer_id,
            InvoiceStateRow.invoice_id != exclude,
            InvoiceStateRow.state.in_([s.value for s in AUTOMATED_STATES]),
        )
        .order_by(InvoiceStateRow.invoice_id)
    )
    if only is not None:
        if not only:
            return []
        stmt = stmt.where(InvoiceStateRow.invoice_id.in_(only))
    return list((await session.execute(stmt)).scalars())


def _account_item(row: InvoiceStateRow, policy: DunningPolicy, now: datetime) -> AccountItem:
    inv = effective_invoice(row, local_today(row, policy, now))
    return AccountItem(
        invoice_id=inv.invoice_id,
        amount=inv.amount,
        currency=inv.currency,
        due_date=inv.due_date,
        days_overdue=inv.days_overdue,
    )


async def _with_account(
    session: AsyncSession, row: InvoiceStateRow, invoice: Invoice, policy: DunningPolicy, now
) -> Invoice:
    others = await _account_rows(session, row.customer_id, exclude=row.invoice_id)
    return invoice.model_copy(
        update={"other_open_invoices": [_account_item(r, policy, now) for r in others]}
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
                resolved_by=audit.WORKFLOW,
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
    event_kind, to_state = transition.event.split(":", 1)[0], row.state
    _on_commit(session, lambda: m.DUNNING_TRANSITIONS.labels(event_kind, to_state).inc())
    for task in created:
        _on_commit(session, lambda t=task.task_type: m.DUNNING_TASKS_OPENED.labels(t).inc())
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
            actor=audit.current_actor(),
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
        actor=audit.current_actor(),
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
                _flush_metrics(session)
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
    # Written before anything that references it. The events, tasks and contact
    # attempts below carry a foreign key to this row, and with no ORM relationship
    # declared, the unit of work does not know to insert the parent first. Postgres
    # rejects the child insert; SQLite without FK enforcement silently accepted it.
    await session.flush()
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
        _flush_metrics(session)
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
    # Contacts not made because a higher-priority invoice of the same customer leads.
    held_for_customer: int = 0
    # Invoices covered by an email or SMS sent about another invoice of the same
    # customer: the account got one reminder listing everything it owes.
    covered_by_account_contact: int = 0
    conflicts: int = 0


async def _tick_one(
    session: AsyncSession,
    invoice_id: str,
    report: TickReport,
    policy: DunningPolicy,
    now: datetime,
    leads: set[str],
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
    if planned is None:
        return
    if planned.channel is not Channel.ESCALATE:
        # One conversation per customer: their highest-priority invoice in active
        # dunning leads, and their other invoices wait behind it. Without this, a
        # low-priority reminder email could land first and push the customer's urgent
        # call out by the minimum contact gap.
        if row.customer_id in leads:
            report.held_for_customer += planned.at <= now
            return
        leads.add(row.customer_id)
    if planned.at > now:
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
        channel = planned.channel.value
        _on_commit(session, lambda: m.DUNNING_CONTACTS.labels(channel).inc())
        await _apply(session, row, on_contact(_snapshot(row), planned.channel, now), now)
        # The reminder is a statement of the account: it lists every invoice the
        # customer has in dunning, so each of them records the contact.
        for other in await _account_rows(session, row.customer_id, exclude=row.invoice_id):
            await _apply(
                session,
                other,
                on_account_contact(
                    _snapshot(other), planned.channel, now, lead_invoice_id=row.invoice_id
                ),
                now,
            )
            await _reschedule(session, other, policy, now)
            report.covered_by_account_contact += 1
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
    with tracer.start_as_current_span("dunning.tick") as span, m.DUNNING_TICK_DURATION.time():
        report = await _tick(now, policy)
        for key, value in report.__dict__.items():
            span.set_attribute(f"dunning.{key}", value)
    m.DUNNING_TICK_LAST_SUCCESS.set_to_current_time()
    return report


async def _tick(now: datetime | None, policy: DunningPolicy | None) -> TickReport:
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    report = TickReport()
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(InvoiceStateRow).where(
                        InvoiceStateRow.state != DunningState.CLOSED.value
                    )
                )
            ).scalars()
        )
    # Highest priority first, so each customer's most urgent invoice claims their
    # contact for this pass.
    ordered = sorted(
        rows,
        key=lambda r: (
            -priority_score(effective_invoice(r, local_today(r, policy, now))),
            r.invoice_id,
        ),
    )
    leads: set[str] = set()
    for invoice_id in (r.invoice_id for r in ordered):
        report.examined += 1
        try:
            async with get_sessionmaker()() as session:
                await _tick_one(session, invoice_id, report, policy, now, leads)
                await session.commit()
                _flush_metrics(session)
        except StaleDataError:
            report.conflicts += 1
            m.DUNNING_TICK_CONFLICTS.inc()
        except Exception:  # noqa: BLE001 - one bad invoice must not stop the pass
            logger.exception("Tick failed for %s", invoice_id)
    return report


@dataclass
class DueCall:
    # Effective (aged, net of payments), carrying the customer's other invoices in
    # dunning: the call is about the account, filed under this invoice.
    invoice: Invoice
    tier: RiskTier
    priority: float
    state: str
    broken_promises: int


async def due_calls(
    *,
    now: datetime | None = None,
    limit: int = 20,
    policy: DunningPolicy | None = None,
    with_account: bool = True,
) -> list[DueCall]:
    """Customers whose cadence wants a call that is permitted right now, highest
    priority first: one entry per customer, filed under their most urgent invoice and
    listing the rest of the account. Re-checked against the contact rules at query
    time: a call that was due at 18:55 is not due at 21:00."""
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
        due: list[tuple[DueCall, InvoiceStateRow]] = []
        for row in rows:
            planned = _plan(row, await _attempts(session, row.customer_id, now), policy, now)
            if planned is None or planned.channel is not Channel.CALL or planned.at > now:
                continue
            today = local_today(row, policy, now)
            invoice = effective_invoice(row, today)
            tier = RiskTier(row.tier)
            call = DueCall(
                invoice=invoice.model_copy(update={"risk_tier": tier}),
                tier=tier,
                priority=priority_score(invoice),
                state=row.state,
                broken_promises=row.broken_promises,
            )
            due.append((call, row))
        due.sort(key=lambda d: (-d[0].priority, d[0].invoice.invoice_id))

        # One call per customer, about their most urgent invoice, covering the rest.
        seen: set[str] = set()
        calls: list[DueCall] = []
        for call, row in due:
            if row.customer_id in seen:
                continue
            seen.add(row.customer_id)
            if with_account:
                call.invoice = await _with_account(session, row, call.invoice, policy, now)
            calls.append(call)
            if len(calls) == limit:
                break
    return calls


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
    # The customer's other invoices the call covered, and whether its promise was
    # applied to all of them (True) or to this invoice only.
    account_invoices: list[str] = field(default_factory=list)
    promise_covers_account: bool = False
    # "Already paid", and the payment was found in the bank feed: the transaction that
    # verified it. None leaves the reconcile task open for the feed or a person.
    payment_verified_by: str | None = None


async def _apply_to_account(
    session: AsyncSession,
    lead: InvoiceStateRow,
    outcome: CallOutcome,
    covered: list[str] | None,
    *,
    source_id: str,
    call_log_id: str | None,
    policy: DunningPolicy,
    now: datetime,
) -> tuple[list[str], bool]:
    """Carry an account call's result to the other invoices it covered.

    A promise for the whole account puts every covered invoice on that promise (each
    with its own verify task, so each closes on its own payment). Anything else - a
    dispute, a partial plan, a refusal, no answer - stays with the lead invoice, and
    the others only record that the customer was contacted about them.
    """
    riders = await _account_rows(session, lead.customer_id, exclude=lead.invoice_id, only=covered)
    if not riders:
        return [], False
    today = local_today(lead, policy, now)
    totals = effective_invoice(lead, today).model_copy(
        update={"other_open_invoices": [_account_item(r, policy, now) for r in riders]}
    ).account_totals()
    whole = promise_covers_account(outcome, totals)
    for other in riders:
        if whole:
            transition = on_call_outcome(
                _snapshot(other),
                outcome,
                call_id=source_id,
                policy=policy,
                now=now,
                today=local_today(other, policy, now),
                current_step_channel=None,
            )
            # The lead's keys are `<call>:<type>`; one call now opens a task per invoice.
            transition.tasks = [
                replace(t, dedupe_key=f"{t.dedupe_key}:{other.invoice_id}")
                for t in transition.tasks
            ]
            transition.detail["lead_invoice"] = lead.invoice_id
        else:
            transition = on_account_contact(
                _snapshot(other),
                Channel.CALL,
                now,
                lead_invoice_id=lead.invoice_id,
                outcome=outcome.outcome.value,
            )
        await _apply(session, other, transition, now, call_log_id=call_log_id)
        await _reschedule(session, other, policy, now)
    return [r.invoice_id for r in riders], whole


async def record_call_outcome(
    invoice_id: str,
    outcome: CallOutcome,
    *,
    call_log_id: str | None = None,
    captured: dict | None = None,
    invoice: Invoice | None = None,
    also_covers: list[str] | None = None,
    cover_account: bool = False,
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> OutcomeReport:
    """Feed a finished call into the workflow.

    `captured` is what the agent's tools recorded during the call; it is reconciled
    against `outcome` (the post-call extraction) and any disagreement opens a QA task.
    `invoice` lets a call about an invoice not yet synced bring it into the workflow.
    `also_covers` names the customer's other invoices the call discussed; it defaults
    to the account the agent was given (`invoice.other_open_invoices`), or with
    `cover_account` to every invoice the customer has in dunning (a call recorded by
    hand, where nobody listed the account up front).
    """
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()
    attempt_id = _new_id()
    source_id = call_log_id or attempt_id
    if also_covers is None and not cover_account:
        also_covers = [i.invoice_id for i in invoice.other_open_invoices] if invoice else []
    span = tracer.start_span("dunning.record_call_outcome")
    span.set_attribute("invoice.id", invoice_id)
    span.set_attribute("call.outcome", outcome.outcome.value)

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

        _on_commit(session, lambda: m.DUNNING_CONTACTS.labels("call").inc())
        if discrepancy:
            _on_commit(session, m.CAPTURE_DISAGREEMENTS.inc)
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
        verified_by = None
        if row.state == DunningState.PAID_UNVERIFIED.value:
            # Imported here: cash application builds on this module's payment path.
            from workflow.cash import verify_reported_payment

            verified_by = await verify_reported_payment(
                session,
                row,
                transition.detail.get("payment_reference"),
                now=now,
                policy=policy,
            )
        if row.state != DunningState.CLOSED.value:
            await _retier(session, row, now, today)
        await _reschedule(session, row, policy, now)
        covered, whole = await _apply_to_account(
            session,
            row,
            outcome,
            also_covers,
            source_id=source_id,
            call_log_id=call_log_id,
            policy=policy,
            now=now,
        )
        return OutcomeReport(
            invoice_id=row.invoice_id,
            state=row.state,
            tier=row.tier,
            tasks_opened=[t.id for t in created],
            discrepancy=discrepancy,
            next_action_channel=row.next_action_channel,
            next_action_at=row.next_action_at,
            account_invoices=covered,
            promise_covers_account=whole,
            payment_verified_by=verified_by,
        )

    try:
        report = await _with_retry(op)
        span.set_attribute("dunning.state", report.state)
        span.set_attribute("dunning.tasks_opened", len(report.tasks_opened))
        return report
    finally:
        span.end()


async def record_payment(
    invoice_id: str,
    amount: Decimal,
    *,
    reference: str = "",
    now: datetime | None = None,
    policy: DunningPolicy | None = None,
) -> InvoiceStateRow:
    """Post a payment to one invoice by hand. Bank lines go through cash application
    (workflow/cash.py), which matches them to invoices first."""
    if amount <= 0:
        raise ValueError("payment amount must be positive")
    now = as_utc(now or datetime.now(UTC))
    policy = policy or get_policy()

    async def op(session: AsyncSession) -> InvoiceStateRow:
        row = await _load(session, invoice_id)
        await apply_payment(session, row, amount, reference=reference, now=now, policy=policy)
        return row

    return await _with_retry(op)


async def apply_payment(
    session: AsyncSession,
    row: InvoiceStateRow,
    amount: Decimal,
    *,
    reference: str,
    now: datetime,
    policy: DunningPolicy,
    settles: bool = False,
    detail: dict | None = None,
) -> None:
    """Reduce an invoice's balance, closing it (and every open follow-up) when paid.

    `settles` closes it even if a remainder is left: cash application decided the
    remainder is within tolerance (bank charges), and it is written off and recorded.
    """
    if row.state == DunningState.CLOSED.value:
        raise TransitionError(f"{row.invoice_id} is already closed ({row.closed_reason})")
    outstanding = max(Decimal(row.amount_outstanding) - amount, Decimal("0"))
    written_off = outstanding if settles else Decimal("0")
    outstanding -= written_off
    row.amount_outstanding = f"{outstanding:.2f}"
    transition = on_payment(_snapshot(row), outstanding, amount=amount, reference=reference)
    if written_off:
        transition.detail["written_off"] = f"{written_off:.2f}"
    transition.detail.update(detail or {})
    await _apply(session, row, transition, now)
    if row.state != DunningState.CLOSED.value:
        await _retier(session, row, now, local_today(row, policy, now))
    await _reschedule(session, row, policy, now)


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
        task.resolved_by = audit.current_actor()
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
        invoice = await _with_account(
            session, row, effective_invoice(row, local_today(row, policy, now)), policy, now
        )
    return InvoiceDetail(row=row, invoice=invoice, tasks=tasks, events=events, attempts=attempts)


@dataclass
class CustomerAccount:
    customer_id: str
    customer_name: str
    invoices: list[tuple[InvoiceStateRow, Invoice]]  # every invoice, any state
    # Per currency, over invoices still in active dunning: what a call would ask for.
    overdue_in_dunning: dict[str, Decimal]
    # Per currency, over every invoice not yet closed.
    outstanding: dict[str, Decimal]
    open_tasks: list[TaskRow]


async def customer_account(customer_id: str, *, now: datetime | None = None) -> CustomerAccount:
    """The customer's whole account: what a collector looks at before calling."""
    now = as_utc(now or datetime.now(UTC))
    policy = get_policy()
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(InvoiceStateRow)
                    .where(InvoiceStateRow.customer_id == customer_id)
                    .order_by(InvoiceStateRow.invoice_id)
                )
            ).scalars()
        )
        if not rows:
            raise NotFoundError(f"No customer {customer_id!r} in the dunning workflow")
        tasks = list(
            (
                await session.execute(
                    select(TaskRow)
                    .where(
                        TaskRow.invoice_id.in_([r.invoice_id for r in rows]),
                        TaskRow.status == "open",
                    )
                    .order_by(TaskRow.due_date)
                )
            ).scalars()
        )
    invoices = [(r, effective_invoice(r, local_today(r, policy, now))) for r in rows]
    in_dunning: dict[str, Decimal] = {}
    outstanding: dict[str, Decimal] = {}
    for r, inv in invoices:
        if r.state == DunningState.CLOSED.value:
            continue
        outstanding[inv.currency] = outstanding.get(inv.currency, Decimal("0")) + inv.amount
        if DunningState(r.state) in AUTOMATED_STATES:
            in_dunning[inv.currency] = in_dunning.get(inv.currency, Decimal("0")) + inv.amount
    return CustomerAccount(
        customer_id=customer_id,
        customer_name=rows[0].invoice.get("customer", {}).get("name", ""),
        invoices=invoices,
        overdue_in_dunning=in_dunning,
        outstanding=outstanding,
        open_tasks=tasks,
    )


async def refresh_metrics(*, now: datetime | None = None) -> None:
    """Recompute the point-in-time workflow gauges from the database.

    Called on each /metrics scrape, so the numbers are as fresh as the scrape and every
    worker reports the same shared state.
    """
    now = as_utc(now or datetime.now(UTC))
    async with get_sessionmaker()() as session:
        states = (
            await session.execute(
                select(InvoiceStateRow.state, InvoiceStateRow.tier, func.count()).group_by(
                    InvoiceStateRow.state, InvoiceStateRow.tier
                )
            )
        ).all()
        open_tasks = (
            await session.execute(
                select(TaskRow.assignee, TaskRow.task_type, func.count())
                .where(TaskRow.status == "open")
                .group_by(TaskRow.assignee, TaskRow.task_type)
            )
        ).all()
        overdue = (
            await session.execute(
                select(TaskRow.assignee, func.count())
                .where(TaskRow.status == "open", TaskRow.due_date < now.date())
                .group_by(TaskRow.assignee)
            )
        ).all()
        balances = (
            await session.execute(
                select(
                    InvoiceStateRow.tier,
                    InvoiceStateRow.amount_outstanding,
                    InvoiceStateRow.invoice,
                ).where(InvoiceStateRow.state != DunningState.CLOSED.value)
            )
        ).all()
        unmatched = await session.scalar(
            select(func.count()).where(BankTransactionRow.status == "unmatched")
        )

    # Cleared first so a state or queue that has emptied reports nothing rather than
    # its last non-zero value.
    m.DUNNING_INVOICES.clear()
    for state, tier, count in states:
        m.DUNNING_INVOICES.labels(state, tier).set(count)
    m.DUNNING_OPEN_TASKS.clear()
    for assignee, task_type, count in open_tasks:
        m.DUNNING_OPEN_TASKS.labels(assignee, task_type).set(count)
    m.DUNNING_OVERDUE_TASKS.clear()
    for assignee, count in overdue:
        m.DUNNING_OVERDUE_TASKS.labels(assignee).set(count)
    by_tier: dict[str, Decimal] = {}
    for tier, amount, invoice in balances:
        stub = Invoice.model_validate(invoice).model_copy(update={"amount": Decimal(amount)})
        by_tier[tier] = by_tier.get(tier, Decimal("0")) + amount_in_inr(stub)
    m.DUNNING_OUTSTANDING.clear()
    for tier, total in by_tier.items():
        m.DUNNING_OUTSTANDING.labels(tier).set(float(total))
    m.DUNNING_CALLS_DUE.set(len(await due_calls(now=now, limit=100_000, with_account=False)))
    m.CASH_UNMATCHED.set(unmatched or 0)


async def has_invoices() -> bool:
    async with get_sessionmaker()() as session:
        return (await session.scalar(select(InvoiceStateRow.invoice_id).limit(1))) is not None


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
