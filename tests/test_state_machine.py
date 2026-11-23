"""The per-invoice dunning state machine. Pure: snapshot + event -> transition."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from shared.models import CallOutcome, OutcomeType, RiskTier
from workflow.policy import Channel, TaskType, load_policy
from workflow.state_machine import (
    DunningState,
    Snapshot,
    TransitionError,
    on_cadence_exhausted,
    on_call_outcome,
    on_contact,
    on_payment,
    on_promise_due,
    on_task_resolved,
    retier,
)

POLICY = load_policy()
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
TODAY = date(2026, 11, 24)


def snap(**kw) -> Snapshot:
    base = dict(
        invoice_id="INV-1",
        state=DunningState.IN_DUNNING,
        tier=RiskTier.MEDIUM,
        cadence_tier=RiskTier.MEDIUM,
        cadence_step=1,
        cadence_started_at=NOW - timedelta(days=5),
    )
    base.update(kw)
    return Snapshot(**base)


def call(s: Snapshot, outcome: OutcomeType, step_channel=Channel.CALL, **fields):
    return on_call_outcome(
        s,
        CallOutcome(outcome=outcome, **fields),
        call_id="CALL-1",
        policy=POLICY,
        now=NOW,
        today=TODAY,
        current_step_channel=step_channel,
    )


class TestCallOutcomes:
    def test_promise_waits_and_opens_a_verify_task(self) -> None:
        promise = date(2026, 11, 30)
        t = call(snap(), OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=promise)
        assert t.snapshot.state is DunningState.PROMISED
        assert t.snapshot.promise_to_pay_date == promise
        [task] = t.tasks
        assert task.task_type is TaskType.VERIFY_PAYMENT
        assert task.assignee == "ar-ops"
        # Due once the grace period has passed, not on the promised day itself.
        assert task.due_date == promise + timedelta(days=POLICY.promise_grace_days)
        assert task.dedupe_key == "CALL-1:verify_payment"

    def test_promise_without_a_date_is_not_a_commitment(self) -> None:
        t = call(snap(), OutcomeType.PROMISE_TO_PAY)
        assert t.snapshot.state is DunningState.IN_DUNNING
        assert t.tasks == []

    def test_dispute_pauses_dunning_and_goes_to_the_disputes_desk(self) -> None:
        t = call(snap(), OutcomeType.DISPUTE_RAISED, dispute_reason="short delivery")
        assert t.snapshot.state is DunningState.DISPUTED
        [task] = t.tasks
        assert (task.task_type, task.assignee) == (TaskType.REVIEW_DISPUTE, "disputes")
        assert task.payload["reason"] == "short delivery"

    def test_already_paid_needs_reconciliation(self) -> None:
        t = call(snap(), OutcomeType.ALREADY_PAID)
        assert t.snapshot.state is DunningState.PAID_UNVERIFIED
        assert t.tasks[0].task_type is TaskType.RECONCILE_PAYMENT

    @pytest.mark.parametrize("tier", [RiskTier.HIGH, RiskTier.CRITICAL])
    def test_refusal_on_a_serious_invoice_escalates(self, tier: RiskTier) -> None:
        t = call(snap(tier=tier, cadence_tier=tier), OutcomeType.REFUSED)
        assert t.snapshot.state is DunningState.ESCALATED
        assert t.tasks[0].task_type is TaskType.ESCALATION_REVIEW

    def test_refusal_on_a_low_invoice_continues_the_cadence(self) -> None:
        t = call(snap(tier=RiskTier.LOW, cadence_tier=RiskTier.LOW), OutcomeType.REFUSED)
        assert t.snapshot.state is DunningState.IN_DUNNING
        assert t.tasks == []

    def test_no_answer_advances_the_cadence(self) -> None:
        t = call(snap(cadence_step=1), OutcomeType.NO_ANSWER)
        assert t.snapshot.cadence_step == 2
        assert t.snapshot.last_contact_at == NOW

    def test_ad_hoc_call_does_not_skip_an_owed_email(self) -> None:
        t = call(snap(cadence_step=0), OutcomeType.NO_ANSWER, step_channel=Channel.EMAIL)
        assert t.snapshot.cadence_step == 0
        assert t.snapshot.last_contact_at == NOW  # still counts for frequency caps

    def test_calls_on_a_closed_invoice_are_ignored(self) -> None:
        closed = snap(state=DunningState.CLOSED)
        t = call(closed, OutcomeType.PROMISE_TO_PAY, promise_to_pay_date=TODAY)
        assert t.snapshot == closed
        assert t.tasks == []


class TestPromises:
    def test_promise_not_broken_within_grace(self) -> None:
        s = snap(state=DunningState.PROMISED, promise_to_pay_date=TODAY - timedelta(days=1))
        assert on_promise_due(s, POLICY, NOW, TODAY) is None

    def test_lapsed_promise_restarts_the_cadence(self) -> None:
        promise = TODAY - timedelta(days=POLICY.promise_grace_days + 1)
        s = snap(state=DunningState.PROMISED, promise_to_pay_date=promise, cadence_step=3)
        t = on_promise_due(s, POLICY, NOW, TODAY)
        assert t is not None
        assert t.snapshot.state is DunningState.IN_DUNNING
        assert t.snapshot.broken_promises == 1
        assert (t.snapshot.cadence_step, t.snapshot.cadence_started_at) == (0, NOW)
        assert (TaskType.VERIFY_PAYMENT, "not_received") in t.close_tasks

    def test_repeated_broken_promises_escalate(self) -> None:
        promise = TODAY - timedelta(days=POLICY.promise_grace_days + 1)
        s = snap(
            state=DunningState.PROMISED,
            promise_to_pay_date=promise,
            broken_promises=POLICY.escalate_after_broken_promises - 1,
        )
        t = on_promise_due(s, POLICY, NOW, TODAY)
        assert t.snapshot.state is DunningState.ESCALATED
        assert t.tasks[0].task_type is TaskType.ESCALATION_REVIEW


class TestCadence:
    def test_contact_advances_one_step(self) -> None:
        t = on_contact(snap(cadence_step=0), Channel.EMAIL, NOW)
        assert (t.snapshot.cadence_step, t.snapshot.last_contact_at) == (1, NOW)

    def test_no_automated_contact_while_paused(self) -> None:
        with pytest.raises(TransitionError):
            on_contact(snap(state=DunningState.DISPUTED), Channel.EMAIL, NOW)

    def test_exhausted_cadence_escalates_with_numbered_task(self) -> None:
        t = on_cadence_exhausted(snap(escalations=1), POLICY, TODAY)
        assert t.snapshot.state is DunningState.ESCALATED
        assert t.tasks[0].dedupe_key == "INV-1:escalation:2"

    def test_retier_moves_active_invoice_onto_new_cadence(self) -> None:
        s = retier(snap(cadence_step=3), RiskTier.HIGH, NOW)
        assert (s.tier, s.cadence_tier, s.cadence_step) == (RiskTier.HIGH, RiskTier.HIGH, 0)

    def test_retier_leaves_paused_invoice_waiting(self) -> None:
        s = retier(snap(state=DunningState.DISPUTED, cadence_step=3), RiskTier.HIGH, NOW)
        assert (s.tier, s.cadence_tier, s.cadence_step) == (RiskTier.HIGH, RiskTier.MEDIUM, 3)


class TestPaymentsAndTasks:
    def test_full_payment_closes_everything(self) -> None:
        t = on_payment(snap(), Decimal("0"), amount=Decimal("500"), reference="UTR1")
        assert t.snapshot.state is DunningState.CLOSED
        assert t.snapshot.closed_reason == "paid"
        assert {task for task, _ in t.close_tasks} >= {
            TaskType.VERIFY_PAYMENT,
            TaskType.REVIEW_DISPUTE,
        }

    def test_partial_payment_keeps_state(self) -> None:
        t = on_payment(snap(), Decimal("100"), amount=Decimal("400"), reference="")
        assert t.snapshot.state is DunningState.IN_DUNNING

    def resolve(self, s, task_type, resolution):
        return on_task_resolved(s, task_type, resolution, policy=POLICY, now=NOW, today=TODAY)

    def test_unknown_resolution_rejected(self) -> None:
        with pytest.raises(TransitionError, match="upheld"):
            self.resolve(snap(state=DunningState.DISPUTED), TaskType.REVIEW_DISPUTE, "maybe")

    def test_upheld_dispute_closes(self) -> None:
        t = self.resolve(snap(state=DunningState.DISPUTED), TaskType.REVIEW_DISPUTE, "upheld")
        assert (t.snapshot.state, t.snapshot.closed_reason) == (
            DunningState.CLOSED,
            "dispute_upheld",
        )

    def test_rejected_dispute_resumes_dunning_from_the_start(self) -> None:
        s = snap(state=DunningState.DISPUTED, cadence_step=3)
        t = self.resolve(s, TaskType.REVIEW_DISPUTE, "rejected")
        assert (t.snapshot.state, t.snapshot.cadence_step) == (DunningState.IN_DUNNING, 0)

    def test_payment_not_found_resumes_dunning(self) -> None:
        s = snap(state=DunningState.PAID_UNVERIFIED)
        t = self.resolve(s, TaskType.RECONCILE_PAYMENT, "not_found")
        assert t.snapshot.state is DunningState.IN_DUNNING

    def test_verify_not_received_breaks_the_promise_immediately(self) -> None:
        s = snap(state=DunningState.PROMISED, promise_to_pay_date=TODAY)
        t = self.resolve(s, TaskType.VERIFY_PAYMENT, "not_received")
        assert t.snapshot.broken_promises == 1

    def test_escalation_write_off_closes(self) -> None:
        s = snap(state=DunningState.ESCALATED)
        t = self.resolve(s, TaskType.ESCALATION_REVIEW, "write_off")
        assert (t.snapshot.state, t.snapshot.closed_reason) == (DunningState.CLOSED, "write_off")

    def test_call_review_never_moves_the_invoice(self) -> None:
        s = snap(state=DunningState.PROMISED, promise_to_pay_date=TODAY)
        t = self.resolve(s, TaskType.REVIEW_CALL, "corrected")
        assert t.snapshot == s

    def test_resolution_on_closed_invoice_is_a_no_op(self) -> None:
        s = replace(snap(), state=DunningState.CLOSED)
        t = self.resolve(s, TaskType.REVIEW_DISPUTE, "rejected")
        assert t.snapshot.state is DunningState.CLOSED
