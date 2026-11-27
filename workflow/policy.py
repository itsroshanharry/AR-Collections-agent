"""The dunning policy: load, validate and expose `dunning_policy.yaml`.

Pure data and validation, no I/O beyond reading the file. Everything that decides what
happens to an invoice reads its rules from the DunningPolicy built here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from shared.models import RiskTier

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "dunning_policy.yaml"


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    CALL = "call"
    ESCALATE = "escalate"


# Channels that interrupt someone, and so are bound by the contact window.
INTRUSIVE_CHANNELS = frozenset({Channel.CALL, Channel.SMS})


class TaskType(StrEnum):
    VERIFY_PAYMENT = "verify_payment"
    REVIEW_DISPUTE = "review_dispute"
    RECONCILE_PAYMENT = "reconcile_payment"
    ESCALATION_REVIEW = "escalation_review"
    REVIEW_CALL = "review_call"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class CadenceStep:
    channel: Channel
    wait_days: int


@dataclass(frozen=True)
class ContactRules:
    max_contacts_per_7_days: int
    max_calls_per_7_days: int
    min_hours_between_contacts: float
    window_start: time
    window_end: time
    contact_days: frozenset[int]


@dataclass(frozen=True)
class DunningPolicy:
    cadences: dict[RiskTier, tuple[CadenceStep, ...]]
    rules: ContactRules
    default_timezones: dict[str, str]
    fallback_timezone: str
    promise_grace_days: int
    escalate_after_broken_promises: int
    task_sla_days: dict[TaskType, int]
    # Cash application: a payment short of the balance by at most this much (per
    # currency) still settles the invoice - bank charges deducted in transit.
    settlement_tolerance: dict[str, Decimal]

    def cadence(self, tier: RiskTier) -> tuple[CadenceStep, ...]:
        return self.cadences[tier]

    def timezone_for(self, timezone: str | None, currency: str) -> ZoneInfo:
        name = timezone or self.default_timezones.get(currency.upper(), self.fallback_timezone)
        return ZoneInfo(name)

    def sla_days(self, task_type: TaskType) -> int:
        return self.task_sla_days.get(task_type, 2)

    def tolerance(self, currency: str) -> Decimal:
        return self.settlement_tolerance.get(currency.upper(), Decimal("0"))


def _parse_time(raw: str, field: str) -> time:
    try:
        return time.fromisoformat(str(raw))
    except ValueError as exc:
        raise PolicyError(f"{field}: expected HH:MM, got {raw!r}") from exc


def parse_policy(raw: dict) -> DunningPolicy:
    """Build and validate a policy from its YAML mapping."""
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a mapping")

    cadences: dict[RiskTier, tuple[CadenceStep, ...]] = {}
    raw_cadences = raw.get("cadences") or {}
    for tier in RiskTier:
        steps_raw = raw_cadences.get(tier.value)
        if not steps_raw:
            raise PolicyError(f"cadences.{tier.value}: every risk tier needs a cadence")
        try:
            steps = tuple(
                CadenceStep(channel=Channel(s["channel"]), wait_days=int(s["wait_days"]))
                for s in steps_raw
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise PolicyError(f"cadences.{tier.value}: malformed step ({exc})") from exc
        if any(s.wait_days < 0 for s in steps):
            raise PolicyError(f"cadences.{tier.value}: wait_days cannot be negative")
        if steps[-1].channel is not Channel.ESCALATE:
            raise PolicyError(
                f"cadences.{tier.value}: must end in 'escalate' so an exhausted cadence "
                "reaches a person instead of looping"
            )
        if any(s.channel is Channel.ESCALATE for s in steps[:-1]):
            raise PolicyError(f"cadences.{tier.value}: 'escalate' may only be the last step")
        cadences[tier] = steps

    rules_raw = raw.get("contact_rules") or {}
    try:
        rules = ContactRules(
            max_contacts_per_7_days=int(rules_raw["max_contacts_per_7_days"]),
            max_calls_per_7_days=int(rules_raw["max_calls_per_7_days"]),
            min_hours_between_contacts=float(rules_raw["min_hours_between_contacts"]),
            window_start=_parse_time(rules_raw["contact_window_start"], "contact_window_start"),
            window_end=_parse_time(rules_raw["contact_window_end"], "contact_window_end"),
            contact_days=frozenset(int(d) for d in rules_raw["contact_days"]),
        )
    except KeyError as exc:
        raise PolicyError(f"contact_rules: missing {exc}") from exc
    if rules.window_start >= rules.window_end:
        raise PolicyError("contact_rules: window start must be before window end")
    if not rules.contact_days or not rules.contact_days <= set(range(1, 8)):
        raise PolicyError("contact_rules.contact_days: ISO weekdays 1-7, at least one")
    if rules.max_contacts_per_7_days < 1 or rules.max_calls_per_7_days < 1:
        raise PolicyError("contact_rules: weekly caps must allow at least one contact")

    default_timezones = {k.upper(): v for k, v in (raw.get("default_timezones") or {}).items()}
    fallback = raw.get("fallback_timezone", "UTC")
    for name in [*default_timezones.values(), fallback]:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PolicyError(f"unknown timezone {name!r}") from exc

    promises = raw.get("promises") or {}
    sla_raw = raw.get("task_sla_days") or {}
    try:
        task_sla_days = {TaskType(k): int(v) for k, v in sla_raw.items()}
    except ValueError as exc:
        raise PolicyError(f"task_sla_days: {exc}") from exc

    cash = raw.get("cash_application") or {}
    try:
        tolerance = {
            k.upper(): Decimal(str(v)) for k, v in (cash.get("settlement_tolerance") or {}).items()
        }
    except InvalidOperation as exc:
        raise PolicyError(f"cash_application.settlement_tolerance: {exc}") from exc
    if any(v < 0 for v in tolerance.values()):
        raise PolicyError("cash_application.settlement_tolerance cannot be negative")

    return DunningPolicy(
        cadences=cadences,
        rules=rules,
        default_timezones=default_timezones,
        fallback_timezone=fallback,
        promise_grace_days=int(promises.get("grace_days", 2)),
        escalate_after_broken_promises=int(promises.get("escalate_after_broken", 2)),
        task_sla_days=task_sla_days,
        settlement_tolerance=tolerance,
    )


def load_policy(path: Path | None = None) -> DunningPolicy:
    path = path or DEFAULT_POLICY_PATH
    return parse_policy(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def get_policy() -> DunningPolicy:
    return load_policy()
