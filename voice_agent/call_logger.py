"""Persist call transcripts, outcomes and judge scores, and flag weak prompt versions.

The continuous-refinement loop lives here: every call is scored against the same rubric
the A/B dashboard uses, tagged with the prompt version that produced it, and written to
the same database. That means a prompt regression shows up in production call logs using
the identical metric the offline experiment reported, rather than a parallel one that
has to be reconciled by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Integer, cast, func, select

from ab_dashboard.db import CallLogRow, get_sessionmaker
from shared.models import CallOutcome, Invoice, JudgeScore

logger = logging.getLogger(__name__)

# A prompt version needs this many scored calls before its mean is worth acting on.
MIN_CALLS_FOR_SIGNAL = 10
# Composite is a 1-5 mean; below this the variant is underperforming.
UNDERPERFORMING_COMPOSITE = 3.5


@dataclass
class PromptHealth:
    prompt_id: str
    call_count: int
    mean_composite: float
    hallucination_rate: float
    underperforming: bool
    reason: str


async def log_call(
    invoice: Invoice,
    prompt_id: str,
    transcript: str,
    outcome: CallOutcome | None,
    score: JudgeScore | None,
    *,
    mode: str = "simulate",
    duration_ms: float = 0.0,
    provider: str = "",
) -> str:
    """Write one call. Returns the row id."""
    async with get_sessionmaker()() as session:
        row = CallLogRow(
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer.customer_id,
            prompt_id=prompt_id,
            risk_tier=invoice.risk_tier.value if invoice.risk_tier else None,
            mode=mode,
            transcript=transcript,
            outcome=outcome.outcome.value if outcome else None,
            promise_to_pay_date=(
                outcome.promise_to_pay_date.isoformat()
                if outcome and outcome.promise_to_pay_date
                else None
            ),
            outcome_confidence=outcome.confidence if outcome else 0.0,
            scores={k.value: v for k, v in (score.scores if score else {}).items()},
            composite=score.composite if score else 0.0,
            hallucinated=bool(score and score.hallucinated),
            hallucinated_figures=list(score.hallucinated_figures) if score else [],
            compliance_violations=list(score.compliance_violations) if score else [],
            duration_ms=duration_ms,
            provider=provider,
        )
        session.add(row)
        await session.commit()
        logger.info(
            "Logged call %s: invoice=%s prompt=%s outcome=%s composite=%.2f",
            row.id,
            invoice.invoice_id,
            prompt_id,
            row.outcome,
            row.composite,
        )
        return row.id


async def prompt_health(prompt_id: str) -> PromptHealth:
    """Rolling quality signal for one prompt version.

    Reports `underperforming` only once there are enough calls to mean anything - a
    single bad call is noise, and auto-flagging on it would make the signal useless.
    """
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(
                func.count(CallLogRow.id),
                func.avg(CallLogRow.composite),
                # Postgres refuses to SUM a boolean; SQLite would allow it. Cast so the
                # same query runs on both.
                func.sum(cast(CallLogRow.hallucinated, Integer)),
            ).where(CallLogRow.prompt_id == prompt_id)
        )
        count, mean_composite, hallucinations = result.one()

    count = int(count or 0)
    mean_composite = float(mean_composite or 0.0)
    hallucination_rate = float(hallucinations or 0) / count if count else 0.0

    if count < MIN_CALLS_FOR_SIGNAL:
        return PromptHealth(
            prompt_id=prompt_id,
            call_count=count,
            mean_composite=round(mean_composite, 3),
            hallucination_rate=round(hallucination_rate, 3),
            underperforming=False,
            reason=f"Only {count} call(s); need {MIN_CALLS_FOR_SIGNAL} for a signal.",
        )

    if mean_composite < UNDERPERFORMING_COMPOSITE:
        reason = (
            f"Mean composite {mean_composite:.2f} is below {UNDERPERFORMING_COMPOSITE} "
            f"over {count} calls."
        )
        underperforming = True
    elif hallucination_rate > 0.05:
        reason = f"Hallucination rate {hallucination_rate:.1%} over {count} calls."
        underperforming = True
    else:
        reason = f"Healthy over {count} calls."
        underperforming = False

    if underperforming:
        logger.warning("Prompt %s flagged as underperforming: %s", prompt_id, reason)

    return PromptHealth(
        prompt_id=prompt_id,
        call_count=count,
        mean_composite=round(mean_composite, 3),
        hallucination_rate=round(hallucination_rate, 3),
        underperforming=underperforming,
        reason=reason,
    )
