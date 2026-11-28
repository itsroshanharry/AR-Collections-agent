"""Experiment lifecycle: launch a comparison, poll it, read the results."""

from __future__ import annotations

import logging
from datetime import date, datetime

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
from shared.eval.eval_set import EvalSetError, cached_eval_set
from shared.eval.metrics import injection_held
from shared.eval.stats import compare

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


class TagBreakdownOut(BaseModel):
    """How each variant did on the scenarios carrying one tag."""

    scenarios: int
    a_wins: int
    b_wins: int
    ties: int
    a_outcome_accuracy: float
    b_outcome_accuracy: float


class SignificanceOut(BaseModel):
    """Whether the A/B difference is distinguishable from noise. See shared/eval/stats.py."""

    decisive: int = Field(description="Scenarios with a winner (ties excluded from the test)")
    ties: int
    p_value: float = Field(description="Exact two-sided sign test on head-to-head wins")
    significant: bool = Field(description="p < 0.05")
    win_rate_ci_a: tuple[float, float] = Field(description="95% Wilson interval")
    win_rate_ci_b: tuple[float, float]
    composite_diff: float = Field(description="Mean composite A minus B, paired by scenario")
    composite_diff_ci: tuple[float, float] = Field(description="95% paired bootstrap interval")
    interpretation: str


class ResultsOut(BaseModel):
    experiment_id: str
    status: str
    scenario_count: int
    ties: int
    variant_a: VariantSummaryOut
    variant_b: VariantSummaryOut
    adversarial_held_a: int
    adversarial_held_b: int
    significance: SignificanceOut
    by_tag: dict[str, TagBreakdownOut] = Field(
        description="Per-capability breakdown: where a variant wins or fails, not just whether"
    )
    verdict: str
    per_scenario: list[ScenarioResultOut]


class VariantRunOut(BaseModel):
    """One variant's run of one scenario: the transcript and everything scored on it."""

    label: str
    prompt_id: str
    transcript: str
    scores: dict[str, float]
    composite: float
    judge_rationale: str
    hallucinated: bool
    hallucinated_figures: list[str]
    compliance_violations: list[str]
    extracted_outcome: str | None
    outcome_match: bool
    promise_date_match: bool | None
    total_latency_ms: float
    mean_turn_latency_ms: float
    provider: str
    error: str | None


class ScenarioDetailOut(BaseModel):
    """Both variants' transcripts for one scenario, and why the judge picked a winner."""

    experiment_id: str
    scenario_id: str
    description: str | None = Field(description="From the eval set; None if since removed")
    customer_persona: str | None
    expected_outcome: str | None
    expected_promise_date: date | None
    is_adversarial: bool
    tags: list[str]
    pairwise_winner: str | None
    pairwise_reason: str
    presented_a_first: bool | None = Field(
        description="Whether the judge saw A's transcript first (order is randomised)"
    )
    variant_a: VariantRunOut | None
    variant_b: VariantRunOut | None


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

    tag_stats: dict[str, dict[str, float]] = {}
    for scenario in per_scenario:
        pair = by_scenario[scenario.scenario_id]
        for tag in pair["A"].tags or []:
            t = tag_stats.setdefault(tag, {"n": 0, "A": 0, "B": 0, "tie": 0, "a_ok": 0, "b_ok": 0})
            t["n"] += 1
            t[scenario.pairwise_winner] += 1
            t["a_ok"] += pair["A"].outcome_match and not pair["A"].error
            t["b_ok"] += pair["B"].outcome_match and not pair["B"].error
    by_tag = {
        tag: TagBreakdownOut(
            scenarios=int(t["n"]),
            a_wins=int(t["A"]),
            b_wins=int(t["B"]),
            ties=int(t["tie"]),
            a_outcome_accuracy=round(t["a_ok"] / t["n"], 3),
            b_outcome_accuracy=round(t["b_ok"] / t["n"], 3),
        )
        for tag, t in sorted(tag_stats.items())
    }

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
    # Composite difference is paired by scenario, and only over pairs where both ran.
    paired = [
        (pair["A"].composite, pair["B"].composite)
        for pair in by_scenario.values()
        if pair.get("A") and pair.get("B") and not pair["A"].error and not pair["B"].error
    ]
    sig = compare(
        summary_a.wins,
        summary_b.wins,
        tie_count,
        [a for a, _ in paired],
        [b for _, b in paired],
    )
    qualifier = f"p={sig.p_value:.3f}" + ("" if sig.significant else ", not significant")
    if summary_a.wins > summary_b.wins:
        verdict = (
            f"{summary_a.prompt_id} wins "
            f"({summary_a.wins}-{summary_b.wins}, {tie_count} ties; {qualifier})"
        )
    elif summary_b.wins > summary_a.wins:
        verdict = (
            f"{summary_b.prompt_id} wins "
            f"({summary_b.wins}-{summary_a.wins}, {tie_count} ties; {qualifier})"
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
        significance=SignificanceOut(**sig.__dict__),
        by_tag=by_tag,
        verdict=verdict,
        per_scenario=per_scenario,
    )


def _run_out(row: ResultRow | None) -> VariantRunOut | None:
    if row is None:
        return None
    return VariantRunOut(
        label=row.variant_label,
        prompt_id=row.prompt_id,
        transcript=row.transcript,
        scores=dict(row.scores or {}),
        composite=row.composite,
        judge_rationale=row.judge_rationale,
        hallucinated=row.hallucinated,
        hallucinated_figures=[str(f) for f in row.hallucinated_figures or []],
        compliance_violations=[str(v) for v in row.compliance_violations or []],
        extracted_outcome=row.extracted_outcome,
        outcome_match=row.outcome_match,
        promise_date_match=row.promise_date_match,
        total_latency_ms=row.total_latency_ms,
        mean_turn_latency_ms=row.mean_turn_latency_ms,
        provider=row.provider,
        error=row.error,
    )


@router.get("/{experiment_id}/scenarios/{scenario_id}", response_model=ScenarioDetailOut)
async def scenario_detail(
    experiment_id: str, scenario_id: str, session: AsyncSession = Depends(get_session)
) -> ScenarioDetailOut:
    """Drill into one scenario: both transcripts side by side, each judge score with its
    rationale, the figures flagged as made up, and the head-to-head judge's reason."""
    if await session.get(ExperimentRow, experiment_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No experiment {experiment_id!r}")
    rows = {
        r.variant_label: r
        for r in (
            await session.execute(
                select(ResultRow).where(
                    ResultRow.experiment_id == experiment_id,
                    ResultRow.scenario_id == scenario_id,
                )
            )
        ).scalars()
    }
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No results for scenario {scenario_id!r} in this run"
        )
    try:
        scenario = next((s for s in cached_eval_set() if s.scenario_id == scenario_id), None)
    except EvalSetError:
        scenario = None
    first = rows.get("A") or rows["B"]
    return ScenarioDetailOut(
        experiment_id=experiment_id,
        scenario_id=scenario_id,
        description=scenario.description if scenario else None,
        customer_persona=scenario.customer_persona if scenario else None,
        expected_outcome=first.expected_outcome,
        expected_promise_date=scenario.expected_promise_date if scenario else None,
        is_adversarial=first.is_adversarial,
        tags=list(first.tags or []),
        pairwise_winner=first.pairwise_winner,
        pairwise_reason=first.pairwise_reason,
        presented_a_first=first.presented_a_first,
        variant_a=_run_out(rows.get("A")),
        variant_b=_run_out(rows.get("B")),
    )
