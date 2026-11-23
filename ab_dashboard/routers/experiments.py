"""Experiment lifecycle: launch a comparison, poll it, read the results."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ab_dashboard import jobs
from ab_dashboard.db import (
    ExperimentRow,
    ExperimentStatus,
    PromptVersionRow,
    ResultRow,
    get_session,
)
from ab_dashboard.experiment_runner import VariantSummary
from shared.config import get_settings
from shared.eval.metrics import injection_held

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/experiments", tags=["experiments"])


class ExperimentIn(BaseModel):
    variant_a_id: str = Field(examples=["collections_formal_v1"])
    variant_b_id: str = Field(examples=["collections_friendly_v2"])
    eval_set: str = "eval_scenarios.yaml"
    seed: int = 42
    limit: int | None = Field(
        default=None, description="Run only the first N scenarios (for a fast smoke test)."
    )


class ExperimentOut(BaseModel):
    id: str
    status: str
    variant_a_id: str
    variant_b_id: str
    eval_set: str
    scenario_count: int
    completed_count: int
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class ScenarioResultOut(BaseModel):
    scenario_id: str
    is_adversarial: bool
    expected_outcome: str | None
    pairwise_winner: str
    a_composite: float
    b_composite: float
    a_outcome: str | None
    b_outcome: str | None
    a_hallucinated: bool
    b_hallucinated: bool
    a_latency_ms: float
    b_latency_ms: float


class VariantSummaryOut(BaseModel):
    prompt_id: str
    label: str
    wins: int
    win_rate: float
    mean_composite: float
    mean_latency_ms: float
    p95_latency_ms: float
    hallucination_rate: float
    outcome_accuracy: float
    promise_date_accuracy: float | None
    compliance_failures: int
    errors: int
    dimension_means: dict[str, float]


class ResultsOut(BaseModel):
    experiment_id: str
    status: str
    scenario_count: int
    ties: int
    variant_a: VariantSummaryOut
    variant_b: VariantSummaryOut
    adversarial_held_a: int
    adversarial_held_b: int
    verdict: str
    per_scenario: list[ScenarioResultOut]


def _experiment_out(row: ExperimentRow) -> ExperimentOut:
    return ExperimentOut(
        id=row.id,
        status=row.status,
        variant_a_id=row.variant_a_id,
        variant_b_id=row.variant_b_id,
        eval_set=row.eval_set,
        scenario_count=row.scenario_count,
        completed_count=row.completed_count,
        error=row.error,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.post("", response_model=ExperimentOut, status_code=status.HTTP_202_ACCEPTED)
async def create_experiment(
    payload: ExperimentIn,
    session: AsyncSession = Depends(get_session),
) -> ExperimentOut:
    """Launch an A-vs-B run over the eval set. Returns immediately; poll for status.

    The run is durable: results are saved per scenario as they finish, and if the
    process restarts mid-run, the run resumes from the last stored scenario.
    """
    if payload.variant_a_id == payload.variant_b_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "variant_a_id and variant_b_id must differ"
        )
    for pid in (payload.variant_a_id, payload.variant_b_id):
        if await session.get(PromptVersionRow, pid) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No prompt {pid!r}. POST /prompts/sync to import the agent's prompts.",
            )

    if not get_settings().has_any_llm_provider:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No LLM provider configured. Set GROQ_API_KEY (or CEREBRAS_API_KEY) in .env",
        )

    experiment = ExperimentRow(
        variant_a_id=payload.variant_a_id,
        variant_b_id=payload.variant_b_id,
        eval_set=payload.eval_set,
        seed=payload.seed,
        scenario_limit=payload.limit,
        status=ExperimentStatus.PENDING,
    )
    session.add(experiment)
    await session.commit()

    # If another worker's reaper gets to it first, that is fine: it runs there instead.
    await jobs.launch(experiment.id)
    await session.refresh(experiment)
    return _experiment_out(experiment)


@router.get("", response_model=list[ExperimentOut])
async def list_experiments(
    session: AsyncSession = Depends(get_session),
) -> list[ExperimentOut]:
    rows = (
        await session.execute(select(ExperimentRow).order_by(ExperimentRow.created_at.desc()))
    ).scalars()
    return [_experiment_out(r) for r in rows]


@router.get("/{experiment_id}", response_model=ExperimentOut)
async def get_experiment(
    experiment_id: str, session: AsyncSession = Depends(get_session)
) -> ExperimentOut:
    row = await session.get(ExperimentRow, experiment_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No experiment {experiment_id!r}")
    return _experiment_out(row)


def _summary_out(summary: VariantSummary, total: int) -> VariantSummaryOut:
    return VariantSummaryOut(
        prompt_id=summary.prompt_id,
        label=summary.label,
        wins=summary.wins,
        win_rate=round(summary.wins / total, 3) if total else 0.0,
        mean_composite=summary.mean_composite,
        mean_latency_ms=summary.mean_latency_ms,
        p95_latency_ms=summary.p95_latency_ms,
        hallucination_rate=summary.hallucination_rate,
        outcome_accuracy=summary.outcome_accuracy,
        promise_date_accuracy=summary.promise_date_accuracy,
        compliance_failures=summary.compliance_failures,
        errors=summary.errors,
        dimension_means=summary.dimension_means,
    )


def _held(row: ResultRow) -> bool:
    return injection_held(
        errored=row.error is not None,
        hallucinated=row.hallucinated,
        extracted_outcome=row.extracted_outcome,
        compliance_violations=row.compliance_violations or (),
    )


@router.get("/{experiment_id}/results", response_model=ResultsOut)
async def experiment_results(
    experiment_id: str, session: AsyncSession = Depends(get_session)
) -> ResultsOut:
    """Aggregate an experiment from its persisted rows.

    Recomputed from stored results rather than cached at write time, so the headline
    numbers can always be reconciled against the individual transcripts. Results are
    stored per scenario as they finish, so this also works on a run still in progress;
    check `status` before treating the numbers as final.
    """
    experiment = await session.get(ExperimentRow, experiment_id)
    if not experiment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No experiment {experiment_id!r}")

    rows = list(
        (
            await session.execute(
                select(ResultRow)
                .where(ResultRow.experiment_id == experiment_id)
                .order_by(ResultRow.scenario_id, ResultRow.variant_label)
            )
        ).scalars()
    )
    if not rows:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Experiment is {experiment.status}; no results yet.",
        )

    by_scenario: dict[str, dict[str, ResultRow]] = {}
    for row in rows:
        by_scenario.setdefault(row.scenario_id, {})[row.variant_label] = row

    per_scenario: list[ScenarioResultOut] = []
    wins = {"A": 0, "B": 0, "tie": 0}
    for scenario_id, pair in sorted(by_scenario.items()):
        a, b = pair.get("A"), pair.get("B")
        if not a or not b:
            continue
        # The persisted position-blind pairwise verdict is authoritative. Absolute
        # composite scores are only a fallback for when that judge call itself failed -
        # they are far less discriminative when both prompts are competent.
        if a.pairwise_winner in ("A", "B", "tie"):
            winner = a.pairwise_winner
        elif a.composite > b.composite:
            winner = "A"
        elif b.composite > a.composite:
            winner = "B"
        else:
            winner = "tie"
        wins[winner] += 1
        per_scenario.append(
            ScenarioResultOut(
                scenario_id=scenario_id,
                is_adversarial=a.is_adversarial,
                expected_outcome=a.expected_outcome,
                pairwise_winner=winner,
                a_composite=a.composite,
                b_composite=b.composite,
                a_outcome=a.extracted_outcome,
                b_outcome=b.extracted_outcome,
                a_hallucinated=a.hallucinated,
                b_hallucinated=b.hallucinated,
                a_latency_ms=a.total_latency_ms,
                b_latency_ms=b.total_latency_ms,
            )
        )

    total = len(per_scenario)

    def agg(label: str) -> VariantSummaryOut:
        subset = [pair[label] for pair in by_scenario.values() if label in pair]
        ok = [r for r in subset if not r.error]
        n = len(ok) or 1
        latencies = sorted(r.total_latency_ms for r in ok)
        dims: dict[str, list[float]] = {}
        for r in ok:
            for k, v in (r.scores or {}).items():
                dims.setdefault(k, []).append(v)
        # promise_date_match is None for scenarios with no ground-truth date.
        date_cases = [r for r in ok if r.promise_date_match is not None]
        return VariantSummaryOut(
            prompt_id=subset[0].prompt_id if subset else "",
            label=label,
            wins=wins[label],
            win_rate=round(wins[label] / total, 3) if total else 0.0,
            mean_composite=round(sum(r.composite for r in ok) / n, 3),
            mean_latency_ms=round(sum(latencies) / n, 1),
            p95_latency_ms=round(
                latencies[min(int(round(0.95 * (len(latencies) - 1))), len(latencies) - 1)], 1
            )
            if latencies
            else 0.0,
            hallucination_rate=round(sum(r.hallucinated for r in ok) / n, 3),
            outcome_accuracy=round(sum(r.outcome_match for r in ok) / n, 3),
            promise_date_accuracy=(
                round(sum(bool(r.promise_date_match) for r in date_cases) / len(date_cases), 3)
                if date_cases
                else None
            ),
            compliance_failures=sum(1 for r in ok if r.compliance_violations),
            errors=sum(1 for r in subset if r.error),
            dimension_means={k: round(sum(v) / len(v), 2) for k, v in dims.items()},
        )

    summary_a, summary_b = agg("A"), agg("B")
    adversarial = [p for p in by_scenario.values() if p.get("A") and p["A"].is_adversarial]

    tie_count = wins["tie"]
    if summary_a.wins > summary_b.wins:
        verdict = (
            f"{summary_a.prompt_id} wins "
            f"({summary_a.wins}-{summary_b.wins}, {tie_count} ties)"
        )
    elif summary_b.wins > summary_a.wins:
        verdict = (
            f"{summary_b.prompt_id} wins "
            f"({summary_b.wins}-{summary_a.wins}, {tie_count} ties)"
        )
    else:
        verdict = f"No separation ({summary_a.wins}-{summary_b.wins}, {tie_count} ties)"

    return ResultsOut(
        experiment_id=experiment_id,
        status=experiment.status,
        scenario_count=total,
        ties=wins["tie"],
        variant_a=summary_a,
        variant_b=summary_b,
        adversarial_held_a=sum(1 for p in adversarial if _held(p["A"])),
        adversarial_held_b=sum(1 for p in adversarial if _held(p["B"])),
        verdict=verdict,
        per_scenario=per_scenario,
    )
