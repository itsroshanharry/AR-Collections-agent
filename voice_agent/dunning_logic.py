"""Risk tiering and call prioritisation.

Deliberately pure functions with no LLM and no I/O: this is the part of a collections
system that must be auditable and reproducible. A finance team can read these rules;
they cannot read a model's weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from shared.models import Invoice, RiskTier, Tone

# Days-overdue thresholds. Below LOW_MAX it's a courtesy nudge; past CRITICAL_MIN the
# invoice is heading for write-off or legal escalation.
LOW_MAX_DAYS = 15
MEDIUM_MAX_DAYS = 45
HIGH_MAX_DAYS = 90

# An invoice large enough to matter regardless of age, in INR.
MATERIAL_AMOUNT_INR = Decimal("250000")

# Static indicative rates, INR per unit. A real system would pull the booking-date rate
# from the ERP; hardcoding keeps tiering deterministic and testable, which matters more
# here than being current. Exposed so tests can assert the conversion actually happens.
FX_TO_INR: dict[str, Decimal] = {
    "INR": Decimal("1"),
    "USD": Decimal("88"),
    "SGD": Decimal("65"),
    "EUR": Decimal("95"),
    "GBP": Decimal("111"),
}


def amount_in_inr(invoice: Invoice) -> Decimal:
    """Normalise to INR so materiality is comparable across currencies.

    Without this, a 12,750 USD invoice (~11 lakh) is scored as less material than a
    300,000 INR one - the single most likely silent failure in a multi-currency ledger.
    Unknown currencies fall back to 1:1, which under-escalates rather than over-escalates.
    """
    return invoice.amount * FX_TO_INR.get(invoice.currency.upper(), Decimal("1"))

# Repeated silence is itself a risk signal, independent of age.
PERSISTENT_REMINDER_COUNT = 3

TIER_TONE: dict[RiskTier, Tone] = {
    RiskTier.LOW: Tone.GENTLE,
    RiskTier.MEDIUM: Tone.NEUTRAL,
    RiskTier.HIGH: Tone.FIRM,
    RiskTier.CRITICAL: Tone.ESCALATION,
}

_TIER_ORDER = [RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH, RiskTier.CRITICAL]


def _bump(tier: RiskTier, steps: int = 1) -> RiskTier:
    idx = min(_TIER_ORDER.index(tier) + steps, len(_TIER_ORDER) - 1)
    return _TIER_ORDER[idx]


def classify_risk(invoice: Invoice, *, broken_promises: int = 0) -> RiskTier:
    """Assign a risk tier from age, materiality and contact history.

    Age sets the baseline; materiality, ignored reminders and broken promises can
    escalate it. A broken promise-to-pay is the strongest of these signals: the customer
    engaged, committed to a date, and still did not pay. Each one bumps a tier.

    A disputed invoice is capped at MEDIUM regardless of age - chasing a customer
    aggressively over an amount they have formally contested is both bad practice and,
    in several jurisdictions, actionable.
    """
    days = invoice.days_overdue

    if days <= LOW_MAX_DAYS:
        tier = RiskTier.LOW
    elif days <= MEDIUM_MAX_DAYS:
        tier = RiskTier.MEDIUM
    elif days <= HIGH_MAX_DAYS:
        tier = RiskTier.HIGH
    else:
        tier = RiskTier.CRITICAL

    if amount_in_inr(invoice) >= MATERIAL_AMOUNT_INR and tier != RiskTier.CRITICAL:
        tier = _bump(tier)

    if invoice.reminder_count >= PERSISTENT_REMINDER_COUNT and tier != RiskTier.CRITICAL:
        tier = _bump(tier)

    if broken_promises > 0:
        tier = _bump(tier, broken_promises)

    if invoice.dispute_flag:
        tier = min(tier, RiskTier.MEDIUM, key=_TIER_ORDER.index)

    return tier


def tone_for_tier(tier: RiskTier) -> Tone:
    return TIER_TONE[tier]


def priority_score(invoice: Invoice) -> float:
    """Higher = call sooner.

    Weighted by exposure (amount), urgency (age) and unresponsiveness (reminders).
    Disputed invoices are pushed down: they need a human, not a dunning call.
    """
    tier_weight = {
        RiskTier.LOW: 1.0,
        RiskTier.MEDIUM: 2.0,
        RiskTier.HIGH: 3.5,
        RiskTier.CRITICAL: 5.0,
    }[classify_risk(invoice)]

    # Square-root scaling so a single huge invoice doesn't monopolise the queue.
    # INR-normalised for the same reason materiality is.
    exposure = float(amount_in_inr(invoice)) ** 0.5
    urgency = min(invoice.days_overdue, 180) / 30.0
    unresponsive = 1.0 + (0.15 * min(invoice.reminder_count, 6))

    score = tier_weight * exposure * (1.0 + urgency) * unresponsive
    if invoice.dispute_flag:
        score *= 0.35
    return round(score, 2)


@dataclass(frozen=True)
class QueuedCall:
    invoice: Invoice
    tier: RiskTier
    tone: Tone
    priority: float


def build_call_queue(invoices: list[Invoice], *, limit: int | None = None) -> list[QueuedCall]:
    """Prioritised call list, highest-value first."""
    queue = [
        QueuedCall(
            invoice=inv,
            tier=classify_risk(inv),
            tone=tone_for_tier(classify_risk(inv)),
            priority=priority_score(inv),
        )
        for inv in invoices
        if inv.days_overdue > 0
    ]
    # Tie-break on invoice_id so the queue is deterministic for a fixed input.
    queue.sort(key=lambda q: (-q.priority, q.invoice.invoice_id))
    return queue[:limit] if limit else queue
