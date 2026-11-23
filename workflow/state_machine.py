"""The per-invoice dunning state machine.

Pure functions: each takes the invoice's current Snapshot plus an event (a call outcome,
a payment, a broken promise, a resolved task) and returns a Transition - the new
snapshot, the follow-up tasks to open, and the tasks to close. No database, no clock
reads; the caller passes `now`. That is what lets every rule be unit-tested and every
decision be replayed from the audit trail.

States:

    new ──► in_dunning ──► promised ──► closed (paid)
               │  ▲            │
               │  └── broken ◄─┘   (bumps the risk tier; repeated → escalated)
               ├──► disputed ────────► closed (upheld) | in_dunning (rejected)
               ├──► paid_unverified ─► closed (confirmed) | in_dunning (not found)
               └──► escalated ───────► closed (write-off / legal) | in_dunning (resume)

Only `new` and `in_dunning` receive automated contact. Every other state is waiting on
a person or a date, and automated dunning is paused until it resolves.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from shared.models import CallOutcome, OutcomeType, RiskTier
from workflow.policy import Channel, DunningPolicy, TaskType


class DunningState(StrEnum):
    NEW = "new"
    IN_DUNNING = "in_dunning"
    PROMISED = "promised"
    DISPUTED = "disputed"
    PAID_UNVERIFIED = "paid_unverified"
    ESCALATED = "escalated"
    CLOSED = "closed"


AUTOMATED_STATES = frozenset({DunningState.NEW, DunningState.IN_DUNNING})

# Which team's queue each follow-up lands in.
TASK_QUEUES: dict[TaskType, str] = {
    TaskType.VERIFY_PAYMENT: "ar-ops",
    TaskType.REVIEW_DISPUTE: "disputes",
    TaskType.RECONCILE_PAYMENT: "ar-ops",
    TaskType.ESCALATION_REVIEW: "collections-lead",
    TaskType.REVIEW_CALL: "qa",
}

# The resolutions a person may record on each task type. Anything else is rejected at
# the API, so a typo cannot silently leave an invoice in the wrong state.
RESOLUTIONS: dict[TaskType, frozenset[str]] = {
    TaskType.VERIFY_PAYMENT: frozenset({"received", "not_received"}),
    TaskType.REVIEW_DISPUTE: frozenset({"upheld", "rejected"}),
    TaskType.RECONCILE_PAYMENT: frozenset({"confirmed", "not_found"}),
    TaskType.ESCALATION_REVIEW: frozenset({"resume_dunning", "write_off", "legal"}),
    TaskType.REVIEW_CALL: frozenset({"confirmed", "corrected"}),
}

# Tasks made moot once an invoice is settled.
SETTLEMENT_CLOSES = (
    TaskType.VERIFY_PAYMENT,
    TaskType.RECONCILE_PAYMENT,
    TaskType.REVIEW_DISPUTE,
    TaskType.ESCALATION_REVIEW,
)


@dataclass(frozen=True)
class Snapshot:
    invoice_id: str
    state: DunningState
    tier: RiskTier
    cadence_tier: RiskTier
    cadence_step: int
    cadence_started_at: datetime
    last_contact_at: datetime | None = None
    promise_to_pay_date: date | None = None
    broken_promises: int = 0
    escalations: int = 0
    closed_reason: str | None = None

    @property
    def automated(self) -> bool:
        return self.state in AUTOMATED_STATES


@dataclass(frozen=True)
class TaskSpec:
    task_type: TaskType
    title: str
    due_date: date
    dedupe_key: str
    payload: dict = field(default_factory=dict)

    @property
    def assignee(self) -> str:
        return TASK_QUEUES[self.task_type]


@dataclass
class Transition:
    snapshot: Snapshot
    event: str
    detail: dict = field(default_factory=dict)
    tasks: list[TaskSpec] = field(default_factory=list)
    # (task type, resolution) - close every open task of that type with that resolution.
    close_tasks: list[tuple[TaskType, str]] = field(default_factory=list)


class TransitionError(ValueError):
    """An event that is not valid for the invoice's current state."""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _restart_cadence(snap: Snapshot, now: datetime, **changes) -> Snapshot:
    return replace(
        snap,
        state=DunningState.IN_DUNNING,
        cadence_tier=snap.tier,
        cadence_step=0,
        cadence_started_at=now,
        promise_to_pay_date=None,
        **changes,
    )


def _escalate(
    snap: Snapshot, reason: str, policy: DunningPolicy, today: date, event: str, **detail
) -> Transition:
    n = snap.escalations + 1
    return Transition(
        snapshot=replace(snap, state=DunningState.ESCALATED, escalations=n),
        event=event,
        detail={"reason": reason, **detail},
        tasks=[
            TaskSpec(
                task_type=TaskType.ESCALATION_REVIEW,
                title=f"Escalated: {reason}",
                due_date=today + timedelta(days=policy.sla_days(TaskType.ESCALATION_REVIEW)),
                # Numbered, so a second escalation after a resume opens a fresh task.
                dedupe_key=f"{snap.invoice_id}:escalation:{n}",
                payload={"reason": reason},
            )
        ],
    )


def _close(snap: Snapshot, reason: str, event: str, **detail) -> Transition:
    return Transition(
        snapshot=replace(
            snap, state=DunningState.CLOSED, closed_reason=reason, promise_to_pay_date=None
        ),
        event=event,
        detail={"reason": reason, **detail},
        close_tasks=[(t, reason) for t in SETTLEMENT_CLOSES],
    )


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def retier(snap: Snapshot, tier: RiskTier, now: datetime) -> Snapshot:
    """Apply a recomputed risk tier. An invoice in active dunning that changes tier moves
    onto the new tier's cadence from its first step; one that is paused keeps waiting
    and picks up the new cadence when it resumes."""
    snap = replace(snap, tier=tier)
    if snap.automated and tier is not snap.cadence_tier:
        snap = replace(snap, cadence_tier=tier, cadence_step=0, cadence_started_at=now)
    return snap


def on_contact(snap: Snapshot, channel: Channel, now: datetime) -> Transition:
    """An automated cadence step went out (email or SMS sent)."""
    if not snap.automated:
        raise TransitionError(f"no automated contact while {snap.state.value}")
    return Transition(
        snapshot=replace(
            snap,
            state=DunningState.IN_DUNNING,
            cadence_step=snap.cadence_step + 1,
            last_contact_at=now,
        ),
        event=f"contact:{channel.value}",
        detail={"step": snap.cadence_step},
    )


def on_cadence_exhausted(snap: Snapshot, policy: DunningPolicy, today: date) -> Transition:
    """The cadence reached its final `escalate` step."""
    return _escalate(
        snap,
        f"{snap.cadence_tier.value} cadence completed without resolution",
        policy,
        today,
        event="escalated:cadence_exhausted",
    )


def on_call_outcome(
    snap: Snapshot,
    outcome: CallOutcome,
    *,
    call_id: str,
    policy: DunningPolicy,
    now: datetime,
    today: date,
    current_step_channel: Channel | None,
    payment_reference: str | None = None,
) -> Transition:
    """Route a finished call to its next state and follow-up work.

    `current_step_channel` is the channel the cadence expected next. A call only
    advances the cadence if the cadence was expecting one; an ad-hoc call (a demo, a
    manual dial) still counts as a contact for the frequency rules but does not skip
    an email the playbook still owes.
    """
    if snap.state is DunningState.CLOSED:
        return Transition(snapshot=snap, event="call_ignored:closed", detail={"call_id": call_id})

    step = snap.cadence_step
    if snap.automated and current_step_channel is Channel.CALL:
        step += 1
    base = replace(snap, last_contact_at=now, cadence_step=step)
    detail: dict = {"call_id": call_id, "outcome": outcome.outcome.value}
    kind = outcome.outcome

    if kind in (OutcomeType.PROMISE_TO_PAY, OutcomeType.PARTIAL_PAYMENT):
        if outcome.promise_to_pay_date is None:
            # A "promise" with no date is not a commitment anyone can verify.
            detail["note"] = "commitment without a date; treated as unclear"
            kind = OutcomeType.UNCLEAR
        else:
            promise = outcome.promise_to_pay_date
            check_on = promise + timedelta(days=policy.promise_grace_days)
            payload = {"promise_to_pay_date": promise.isoformat()}
            if outcome.promised_amount is not None:
                payload["promised_amount"] = str(outcome.promised_amount)
            return Transition(
                snapshot=replace(base, state=DunningState.PROMISED, promise_to_pay_date=promise),
                event=f"call:{kind.value}",
                detail={**detail, **payload},
                tasks=[
                    TaskSpec(
                        task_type=TaskType.VERIFY_PAYMENT,
                        title=f"Confirm payment promised for {promise.isoformat()}",
                        due_date=check_on
                        + timedelta(days=policy.sla_days(TaskType.VERIFY_PAYMENT)),
                        dedupe_key=f"{call_id}:{TaskType.VERIFY_PAYMENT.value}",
                        payload=payload,
                    )
                ],
            )

    if kind is OutcomeType.DISPUTE_RAISED:
        payload = {"reason": outcome.dispute_reason or ""}
        if outcome.disputed_amount is not None:
            payload["disputed_amount"] = str(outcome.disputed_amount)
        return Transition(
            snapshot=replace(base, state=DunningState.DISPUTED),
            event="call:dispute_raised",
            detail={**detail, **payload},
            tasks=[
                TaskSpec(
                    task_type=TaskType.REVIEW_DISPUTE,
                    title="Review disputed invoice",
                    due_date=today + timedelta(days=policy.sla_days(TaskType.REVIEW_DISPUTE)),
                    dedupe_key=f"{call_id}:{TaskType.REVIEW_DISPUTE.value}",
                    payload=payload,
                )
            ],
        )

    if kind is OutcomeType.ALREADY_PAID:
        payload = {"payment_reference": payment_reference or ""}
        return Transition(
            snapshot=replace(base, state=DunningState.PAID_UNVERIFIED),
            event="call:already_paid",
            detail={**detail, **payload},
            tasks=[
                TaskSpec(
                    task_type=TaskType.RECONCILE_PAYMENT,
                    title="Reconcile reported payment against bank statement",
                    due_date=today + timedelta(days=policy.sla_days(TaskType.RECONCILE_PAYMENT)),
                    dedupe_key=f"{call_id}:{TaskType.RECONCILE_PAYMENT.value}",
                    payload=payload,
                )
            ],
        )

    if kind is OutcomeType.REFUSED and snap.tier in (RiskTier.HIGH, RiskTier.CRITICAL):
        # A refusal on a seriously overdue invoice is past what more calls will fix.
        return _escalate(
            base, "customer refused to pay", policy, today, event="call:refused", call_id=call_id
        )

    # refused (low/medium), no_answer, unclear: the cadence carries on.
    state = DunningState.IN_DUNNING if snap.automated else snap.state
    return Transition(
        snapshot=replace(base, state=state), event=f"call:{kind.value}", detail=detail
    )


def on_promise_due(
    snap: Snapshot, policy: DunningPolicy, now: datetime, today: date
) -> Transition | None:
    """Has a promise lapsed? Returns the broken-promise transition, or None if not yet."""
    if snap.state is not DunningState.PROMISED or snap.promise_to_pay_date is None:
        return None
    if today <= snap.promise_to_pay_date + timedelta(days=policy.promise_grace_days):
        return None
    return _break_promise(snap, policy, now, today, source="grace period elapsed")


def _break_promise(
    snap: Snapshot, policy: DunningPolicy, now: datetime, today: date, *, source: str
) -> Transition:
    broken = snap.broken_promises + 1
    promised = snap.promise_to_pay_date.isoformat() if snap.promise_to_pay_date else None
    detail = {"promise_to_pay_date": promised, "broken_promises": broken, "source": source}
    if broken >= policy.escalate_after_broken_promises:
        transition = _escalate(
            replace(snap, broken_promises=broken, promise_to_pay_date=None),
            f"{broken} broken payment promises",
            policy,
            today,
            event="promise_broken",
            **detail,
        )
    else:
        # The tier bump itself is applied by the caller, which recomputes the tier with
        # the new broken_promises count; restarting the cadence here means the customer
        # is re-contacted on the (harsher) cadence from its first step.
        transition = Transition(
            snapshot=_restart_cadence(snap, now, broken_promises=broken),
            event="promise_broken",
            detail=detail,
        )
    transition.close_tasks.append((TaskType.VERIFY_PAYMENT, "not_received"))
    return transition


def on_payment(snap: Snapshot, outstanding_after, *, amount, reference: str) -> Transition:
    """A payment landed. Full settlement closes the invoice and every open follow-up."""
    detail = {"amount": str(amount), "reference": reference, "outstanding": str(outstanding_after)}
    if outstanding_after <= 0:
        return _close(snap, "paid", event="payment:settled", **detail)
    return Transition(snapshot=snap, event="payment:partial", detail=detail)


def on_task_resolved(
    snap: Snapshot,
    task_type: TaskType,
    resolution: str,
    *,
    policy: DunningPolicy,
    now: datetime,
    today: date,
) -> Transition:
    """A person resolved a follow-up task. The resolution decides what happens next."""
    if resolution not in RESOLUTIONS[task_type]:
        allowed = ", ".join(sorted(RESOLUTIONS[task_type]))
        raise TransitionError(f"{task_type.value} resolution must be one of: {allowed}")

    event = f"task:{task_type.value}:{resolution}"
    if snap.state is DunningState.CLOSED and task_type is not TaskType.REVIEW_CALL:
        return Transition(snapshot=snap, event=f"{event}:ignored_closed")

    if task_type is TaskType.VERIFY_PAYMENT:
        if resolution == "received":
            return _close(snap, "paid", event=event)
        if snap.state is DunningState.PROMISED:
            return _break_promise(snap, policy, now, today, source="confirmed not received")
        return Transition(snapshot=snap, event=event)

    if task_type is TaskType.REVIEW_DISPUTE:
        if resolution == "upheld":
            return _close(snap, "dispute_upheld", event=event)
        return Transition(snapshot=_restart_cadence(snap, now), event=event)

    if task_type is TaskType.RECONCILE_PAYMENT:
        if resolution == "confirmed":
            return _close(snap, "paid", event=event)
        return Transition(snapshot=_restart_cadence(snap, now), event=event)

    if task_type is TaskType.ESCALATION_REVIEW:
        if resolution == "resume_dunning":
            return Transition(snapshot=_restart_cadence(snap, now), event=event)
        return _close(snap, resolution, event=event)

    # REVIEW_CALL is call-quality QA; it never moves the invoice.
    return Transition(snapshot=snap, event=event)
