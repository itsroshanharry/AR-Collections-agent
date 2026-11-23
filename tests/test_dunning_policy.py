"""The dunning policy file and the contact scheduler. Pure functions, exact answers."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from shared.models import RiskTier
from workflow.policy import (
    DEFAULT_POLICY_PATH,
    CadenceStep,
    Channel,
    PolicyError,
    load_policy,
    parse_policy,
)
from workflow.scheduler import Attempt, next_action, shift_into_window

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
POLICY = load_policy()
RULES = POLICY.rules


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in November 2026, Indian time. 23 Nov 2026 is a Monday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def _raw() -> dict:
    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


class TestPolicyFile:
    def test_shipped_policy_loads(self) -> None:
        assert set(POLICY.cadences) == set(RiskTier)

    def test_every_cadence_ends_in_escalation(self) -> None:
        for steps in POLICY.cadences.values():
            assert steps[-1].channel is Channel.ESCALATE

    def test_harsher_tiers_call_sooner(self) -> None:
        def first_call_day(tier: RiskTier) -> int:
            elapsed = 0
            for step in POLICY.cadence(tier):
                elapsed += step.wait_days
                if step.channel is Channel.CALL:
                    return elapsed
            raise AssertionError("no call step")

        days = [first_call_day(t) for t in RiskTier]
        assert days == sorted(days, reverse=True)

    def test_missing_tier_rejected(self) -> None:
        raw = _raw()
        del raw["cadences"]["critical"]
        with pytest.raises(PolicyError, match="critical"):
            parse_policy(raw)

    def test_cadence_must_end_in_escalate(self) -> None:
        raw = _raw()
        raw["cadences"]["low"] = [{"channel": "email", "wait_days": 0}]
        with pytest.raises(PolicyError, match="must end in 'escalate'"):
            parse_policy(raw)

    def test_escalate_only_as_last_step(self) -> None:
        raw = _raw()
        raw["cadences"]["low"] = [
            {"channel": "escalate", "wait_days": 0},
            {"channel": "escalate", "wait_days": 1},
        ]
        with pytest.raises(PolicyError, match="only be the last"):
            parse_policy(raw)

    def test_unknown_channel_rejected(self) -> None:
        raw = _raw()
        raw["cadences"]["low"][0]["channel"] = "carrier_pigeon"
        with pytest.raises(PolicyError, match="malformed"):
            parse_policy(raw)

    def test_inverted_contact_window_rejected(self) -> None:
        raw = _raw()
        raw["contact_rules"]["contact_window_start"] = "20:00"
        with pytest.raises(PolicyError, match="window"):
            parse_policy(raw)

    def test_unknown_timezone_rejected(self) -> None:
        raw = copy.deepcopy(_raw())
        raw["default_timezones"]["INR"] = "Mars/Olympus_Mons"
        with pytest.raises(PolicyError, match="timezone"):
            parse_policy(raw)

    def test_timezone_falls_back_to_currency_then_default(self) -> None:
        assert POLICY.timezone_for("Europe/Paris", "INR") == ZoneInfo("Europe/Paris")
        assert POLICY.timezone_for(None, "usd") == NY
        assert POLICY.timezone_for(None, "JPY") == ZoneInfo(POLICY.fallback_timezone)


# --------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------

CALL_THEN_EMAIL = (
    CadenceStep(Channel.CALL, 0),
    CadenceStep(Channel.EMAIL, 3),
    CadenceStep(Channel.ESCALATE, 2),
)


def plan(step: int, anchor: datetime, now: datetime, attempts=(), tz=IST, **kw):
    return next_action(
        cadence=CALL_THEN_EMAIL,
        step_index=step,
        anchor=anchor,
        attempts=list(attempts),
        rules=RULES,
        tz=tz,
        now=now,
        **kw,
    )


class TestContactWindow:
    def test_inside_window_is_unchanged(self) -> None:
        at = ist(24, 11)
        assert shift_into_window(at, RULES, IST) == at

    def test_before_opening_waits_for_opening(self) -> None:
        assert shift_into_window(ist(24, 7), RULES, IST) == ist(24, 9)

    def test_after_closing_moves_to_next_morning(self) -> None:
        assert shift_into_window(ist(24, 19, 30), RULES, IST) == ist(25, 9)

    def test_sunday_is_skipped(self) -> None:
        # Saturday 20:00 -> Sunday is not a contact day -> Monday 09:00.
        assert shift_into_window(ist(21, 20), RULES, IST) == ist(23, 9)

    def test_window_is_the_customers_local_time(self) -> None:
        # 22:00 in India is 11:30 in New York: fine to call a US customer.
        at = ist(24, 22)
        assert shift_into_window(at, RULES, NY) == at
        assert shift_into_window(at, RULES, IST) == ist(25, 9)


class TestNextAction:
    def test_first_step_is_due_now(self) -> None:
        now = ist(24, 10)
        action = plan(0, anchor=now, now=now)
        assert (action.channel, action.at, action.reasons) == (Channel.CALL, now, ())

    def test_wait_days_counted_from_anchor(self) -> None:
        anchor = ist(24, 10)
        action = plan(1, anchor=anchor, now=anchor)
        assert action.at == anchor + timedelta(days=3)

    def test_never_earlier_than_now(self) -> None:
        action = plan(1, anchor=ist(1, 10), now=ist(24, 10))
        assert action.at == ist(24, 10)

    def test_call_outside_hours_is_deferred_not_skipped(self) -> None:
        action = plan(0, anchor=ist(24, 21), now=ist(24, 21))
        assert action.channel is Channel.CALL
        assert action.at == ist(25, 9)
        assert "outside the customer's contact hours" in action.reasons

    def test_email_ignores_the_contact_window(self) -> None:
        anchor = ist(21, 22)  # Saturday night
        action = plan(1, anchor=anchor, now=anchor)
        assert action.channel is Channel.EMAIL
        assert action.at == anchor + timedelta(days=3)

    def test_minimum_gap_since_any_contact(self) -> None:
        last = ist(24, 14)  # +20h lands at 10:00 next day, inside calling hours
        action = plan(
            0, anchor=last, now=ist(24, 16), attempts=[Attempt(Channel.EMAIL, last)]
        )
        assert action.at == last + timedelta(hours=RULES.min_hours_between_contacts)
        assert "minimum gap since last contact" in action.reasons

    def test_weekly_call_cap(self) -> None:
        calls = [Attempt(Channel.CALL, ist(20, 10)), Attempt(Channel.CALL, ist(22, 10))]
        action = plan(0, anchor=ist(23, 10), now=ist(24, 10), attempts=calls)
        # Two calls in the last 7 days: the next waits until the first ages out.
        assert action.at == ist(27, 10)
        assert "weekly call cap" in action.reasons

    def test_weekly_contact_cap_counts_every_channel(self) -> None:
        sent = [Attempt(Channel.EMAIL, ist(d, 10)) for d in (18, 19, 20, 21)]
        action = plan(1, anchor=ist(21, 10), now=ist(24, 12), attempts=sent)
        assert action.at == ist(25, 10)
        assert "weekly contact cap" in action.reasons

    def test_do_not_call_turns_calls_into_email(self) -> None:
        now = ist(24, 22)
        action = plan(0, anchor=now, now=now, do_not_call=True)
        assert action.channel is Channel.EMAIL
        assert action.at == now  # email is not bound by calling hours
        assert any("do-not-call" in r for r in action.reasons)

    def test_escalation_is_not_a_contact(self) -> None:
        busy = [Attempt(Channel.CALL, ist(24, 9))]
        action = plan(2, anchor=ist(22, 10), now=ist(24, 23), attempts=busy)
        assert action.channel is Channel.ESCALATE
        assert action.at == ist(24, 23)

    def test_naive_datetimes_are_read_as_utc(self) -> None:
        # SQLite returns timezone-aware columns as naive values.
        naive = datetime(2026, 11, 24, 5, 0)  # 10:30 IST
        action = plan(0, anchor=naive, now=naive)
        assert action.at == naive.replace(tzinfo=UTC)
