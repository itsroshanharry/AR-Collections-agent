"""When may we next contact this customer, and how?

Pure functions over datetimes. The cadence says what the playbook *wants* to do next;
the contact rules say when that is *allowed*. A step that would break a rule is delayed
to the earliest time it no longer does - never skipped, never sent early.

All datetimes are timezone-aware. Storage is UTC; the contact window is evaluated in
the customer's own timezone, because "no calls after 7pm" means 7pm where the phone
rings, not where the server runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from workflow.policy import INTRUSIVE_CHANNELS, CadenceStep, Channel, ContactRules

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Attempt:
    channel: Channel
    at: datetime


@dataclass(frozen=True)
class NextAction:
    channel: Channel
    at: datetime
    step: int
    # Why `at` is later than the cadence alone would put it, for the audit trail.
    reasons: tuple[str, ...] = ()


def as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone-aware columns. Everything we
    store is UTC, so a naive value is UTC by construction."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _release_cap(attempts: Sequence[datetime], cap: int, at: datetime) -> datetime:
    """Earliest time >= `at` with fewer than `cap` attempts in the preceding 7 days."""
    ordered = sorted(attempts)
    for _ in range(len(ordered) + 1):
        window = [a for a in ordered if at - WEEK < a <= at]
        if len(window) < cap:
            return at
        # The window must lose (len - cap + 1) attempts; wait for that one to age out.
        at = window[len(window) - cap] + WEEK
    return at


def shift_into_window(at: datetime, rules: ContactRules, tz: ZoneInfo) -> datetime:
    """Earliest time >= `at` inside the contact window on an allowed weekday."""
    local = at.astimezone(tz)
    for _ in range(8):  # at most a week of disallowed days
        start = local.replace(
            hour=rules.window_start.hour, minute=rules.window_start.minute, second=0, microsecond=0
        )
        end = local.replace(
            hour=rules.window_end.hour, minute=rules.window_end.minute, second=0, microsecond=0
        )
        if local.isoweekday() in rules.contact_days:
            if local < start:
                return start.astimezone(UTC)
            if local < end:
                return local.astimezone(UTC)
        # Too late today, or not a contact day: try tomorrow's opening.
        local = (start + timedelta(days=1)).replace(
            hour=rules.window_start.hour, minute=rules.window_start.minute
        )
    raise ValueError("contact rules allow no contact day")  # unreachable after validation


def next_action(
    *,
    cadence: Sequence[CadenceStep],
    step_index: int,
    anchor: datetime,
    attempts: Sequence[Attempt],
    rules: ContactRules,
    tz: ZoneInfo,
    now: datetime,
    do_not_call: bool = False,
) -> NextAction:
    """The next permitted action for an invoice in active dunning.

    `anchor` is the last contact, or when the invoice entered its current cadence if it
    has not been contacted since. The result is never earlier than `now`.
    """
    index = min(step_index, len(cadence) - 1)
    step = cadence[index]
    channel = step.channel
    reasons: list[str] = []

    if do_not_call and channel in INTRUSIVE_CHANNELS:
        reasons.append(f"do-not-call: {channel.value} replaced by email")
        channel = Channel.EMAIL

    at = max(as_utc(anchor) + timedelta(days=step.wait_days), as_utc(now))

    if channel is Channel.ESCALATE:
        # Handing to a person is not a customer contact; no contact rule applies.
        return NextAction(channel=channel, at=at, step=index, reasons=tuple(reasons))

    history = [as_utc(a.at) for a in attempts]
    if history:
        gap_ok = max(history) + timedelta(hours=rules.min_hours_between_contacts)
        if gap_ok > at:
            at = gap_ok
            reasons.append("minimum gap since last contact")

    capped = _release_cap(history, rules.max_contacts_per_7_days, at)
    if capped > at:
        at = capped
        reasons.append("weekly contact cap")

    if channel is Channel.CALL:
        calls = [as_utc(a.at) for a in attempts if a.channel is Channel.CALL]
        capped = _release_cap(calls, rules.max_calls_per_7_days, at)
        if capped > at:
            at = capped
            reasons.append("weekly call cap")

    if channel in INTRUSIVE_CHANNELS:
        windowed = shift_into_window(at, rules, tz)
        if windowed > at:
            at = windowed
            reasons.append("outside the customer's contact hours")

    return NextAction(channel=channel, at=at, step=index, reasons=tuple(reasons))
