"""Tier boundaries and prioritisation. Pure functions, no LLM - these must be exact."""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.models import RiskTier, Tone
from tests.conftest import make_invoice
from voice_agent.dunning_logic import (
    HIGH_MAX_DAYS,
    LOW_MAX_DAYS,
    MATERIAL_AMOUNT_INR,
    MEDIUM_MAX_DAYS,
    amount_in_inr,
    build_call_queue,
    classify_risk,
    priority_score,
    tone_for_tier,
)

SMALL = Decimal("1000")  # well under the materiality threshold


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, RiskTier.LOW),
        (LOW_MAX_DAYS, RiskTier.LOW),
        (LOW_MAX_DAYS + 1, RiskTier.MEDIUM),
        (MEDIUM_MAX_DAYS, RiskTier.MEDIUM),
        (MEDIUM_MAX_DAYS + 1, RiskTier.HIGH),
        (HIGH_MAX_DAYS, RiskTier.HIGH),
        (HIGH_MAX_DAYS + 1, RiskTier.CRITICAL),
        (400, RiskTier.CRITICAL),
    ],
)
def test_age_sets_baseline_tier(days: int, expected: RiskTier) -> None:
    assert classify_risk(make_invoice(days_overdue=days, amount=SMALL)) == expected


def test_material_amount_escalates_one_tier() -> None:
    small = make_invoice(days_overdue=10, amount=SMALL)
    large = make_invoice(days_overdue=10, amount=MATERIAL_AMOUNT_INR)
    assert classify_risk(small) is RiskTier.LOW
    assert classify_risk(large) is RiskTier.MEDIUM


def test_persistent_reminders_escalate_one_tier() -> None:
    quiet = make_invoice(days_overdue=10, amount=SMALL, reminder_count=0)
    ignored = make_invoice(days_overdue=10, amount=SMALL, reminder_count=3)
    assert classify_risk(quiet) is RiskTier.LOW
    assert classify_risk(ignored) is RiskTier.MEDIUM


def test_escalations_stack_but_cap_at_critical() -> None:
    inv = make_invoice(days_overdue=400, amount=MATERIAL_AMOUNT_INR * 10, reminder_count=5)
    assert classify_risk(inv) is RiskTier.CRITICAL


def test_dispute_caps_tier_at_medium() -> None:
    """Chasing a formally disputed invoice hard is both bad practice and a legal risk."""
    inv = make_invoice(days_overdue=400, amount=Decimal("5000000"), dispute_flag=True)
    assert classify_risk(inv) is RiskTier.MEDIUM


def test_dispute_does_not_raise_a_low_tier() -> None:
    inv = make_invoice(days_overdue=3, amount=SMALL, dispute_flag=True)
    assert classify_risk(inv) is RiskTier.LOW


class TestCurrencyNormalisation:
    """A foreign-currency invoice must be judged on its INR value.

    Without this, 12,750 USD (~11 lakh) scores as less material than 300,000 INR - the
    most likely silent failure mode in a multi-currency ledger.
    """

    def test_usd_converted_before_materiality_check(self) -> None:
        assert amount_in_inr(make_invoice(amount=Decimal("12750"), currency="USD")) > (
            MATERIAL_AMOUNT_INR
        )

    def test_small_usd_invoice_escalates_where_same_number_in_inr_does_not(self) -> None:
        usd = make_invoice(days_overdue=10, amount=Decimal("12750"), currency="USD")
        inr = make_invoice(days_overdue=10, amount=Decimal("12750"), currency="INR")
        assert classify_risk(usd) is RiskTier.MEDIUM
        assert classify_risk(inr) is RiskTier.LOW

    def test_unknown_currency_falls_back_to_one_to_one(self) -> None:
        inv = make_invoice(amount=Decimal("1000"), currency="XYZ")
        assert amount_in_inr(inv) == Decimal("1000")

    def test_priority_uses_inr_normalised_exposure(self) -> None:
        usd = make_invoice(invoice_id="A", amount=Decimal("10000"), currency="USD")
        inr = make_invoice(invoice_id="B", amount=Decimal("10000"), currency="INR")
        assert priority_score(usd) > priority_score(inr)


def test_tone_maps_every_tier() -> None:
    assert {tone_for_tier(t) for t in RiskTier} == {
        Tone.GENTLE,
        Tone.NEUTRAL,
        Tone.FIRM,
        Tone.ESCALATION,
    }


class TestCallQueue:
    def test_excludes_invoices_not_yet_overdue(self) -> None:
        queue = build_call_queue([make_invoice(days_overdue=0)])
        assert queue == []

    def test_orders_by_descending_priority(self) -> None:
        invoices = [
            make_invoice(invoice_id="SMALL", days_overdue=5, amount=SMALL),
            make_invoice(invoice_id="BIG", days_overdue=200, amount=Decimal("900000")),
        ]
        assert [q.invoice.invoice_id for q in build_call_queue(invoices)] == ["BIG", "SMALL"]

    def test_disputed_invoices_are_deprioritised(self) -> None:
        clean = make_invoice(invoice_id="CLEAN", days_overdue=60, amount=Decimal("200000"))
        disputed = make_invoice(
            invoice_id="DISPUTED",
            days_overdue=60,
            amount=Decimal("200000"),
            dispute_flag=True,
        )
        assert priority_score(disputed) < priority_score(clean)

    def test_is_deterministic_for_identical_priorities(self) -> None:
        invoices = [
            make_invoice(invoice_id="B", days_overdue=30),
            make_invoice(invoice_id="A", days_overdue=30),
        ]
        first = [q.invoice.invoice_id for q in build_call_queue(invoices)]
        second = [q.invoice.invoice_id for q in build_call_queue(list(reversed(invoices)))]
        assert first == second == ["A", "B"]

    def test_respects_limit(self) -> None:
        invoices = [make_invoice(invoice_id=f"I{i}", days_overdue=10 + i) for i in range(10)]
        assert len(build_call_queue(invoices, limit=3)) == 3


class TestBrokenPromises:
    def test_each_broken_promise_bumps_a_tier(self) -> None:
        inv = make_invoice(days_overdue=10, amount=Decimal("1000"))  # baseline LOW
        assert classify_risk(inv) is RiskTier.LOW
        assert classify_risk(inv, broken_promises=1) is RiskTier.MEDIUM
        assert classify_risk(inv, broken_promises=2) is RiskTier.HIGH

    def test_bump_saturates_at_critical(self) -> None:
        inv = make_invoice(days_overdue=80, amount=Decimal("1000"))  # baseline HIGH
        assert classify_risk(inv, broken_promises=5) is RiskTier.CRITICAL

    def test_dispute_cap_still_wins(self) -> None:
        inv = make_invoice(days_overdue=80, amount=Decimal("1000"), dispute_flag=True)
        assert classify_risk(inv, broken_promises=3) is RiskTier.MEDIUM
