"""Run two prompt versions against the fixed eval set and aggregate the comparison.

Per scenario: simulate a call with each variant, score each transcript independently
against the rubric, extract the structured outcome and check it against ground truth,
then run a position-blind head-to-head preference judgement.

Two metrics that would be easy to get wrong and are handled explicitly here:

  * Win-rate comes from the pairwise judge with presentation order randomised per
    scenario from a fixed seed. A fixed order would make the number measure position
    bias rather than prompt quality.
  * Extraction accuracy is scored against the eval set's ground truth, not against the
    judge's opinion. An agent and a judge can agree with each other and both be wrong.
"""

from __future__ import annotations

import asyncio
import logging
import random
import statistics
from dataclasses import dataclass, field
from datetime import date

from shared.eval.judge import judge_pairwise, judge_transcript
from shared.llm_client import LLMClient
from shared.models import (
    EvalScenario,
    JudgeScore,
    OutcomeType,
    PromptVersion,
    RiskTier,
)
from voice_agent.dunning_logic import classify_risk, tone_for_tier
from voice_agent.outcome_extractor import extract_outcome
from voice_agent.simulator import simulate_call

logger = logging.getLogger(__name__)

# Reference date used to resolve spoken dates ("the eighth of August"). Fixed so an
# experiment re-run months later still scores promise dates the same way.
EVAL_REFERENCE_DATE = date(2026, 8, 1)


@dataclass
class VariantRun:
    """One variant's result for one scenario."""

    scenario_id: str
    variant_label: str
    prompt_id: str
    transcript: str = ""
    score: JudgeScore | None = None
    extracted_outcome: OutcomeType | None = None
    promise_to_pay_date: date | None = None
    total_latency_ms: float = 0.0
    mean_turn_latency_ms: float = 0.0
    provider: str = ""
    error: str | None = None

    @property
    def composite(self) -> float:
        return self.score.composite if self.score else 0.0

    @property
    def hallucinated(self) -> bool:
        return bool(self.score and self.score.hallucinated)


@dataclass
class ScenarioComparison:
    scenario_id: str
    is_adversarial: bool
    expected_outcome: OutcomeType
    a: VariantRun
    b: VariantRun
    pairwise_winner: str = "tie"  # "A" | "B" | "tie"
    pairwise_reason: str = ""
    presented_a_first: bool = True


@dataclass
class VariantSummary:
    prompt_id: str
    label: str
    wins: int = 0
    mean_composite: float = 0.0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    hallucination_rate: float = 0.0
    outcome_accuracy: float = 0.0
    promise_date_accuracy: float | None = None
    compliance_failures: int = 0
    errors: int = 0
    dimension_means: dict[str, float] = field(default_factory=dict)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round((pct / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


async def _run_variant(
    client: LLMClient,
    prompt: PromptVersion,
    scenario: EvalScenario,
    label: str,
) -> VariantRun:
    run = VariantRun(
        scenario_id=scenario.scenario_id, variant_label=label, prompt_id=prompt.id
    )
    tier: RiskTier = classify_risk(scenario.invoice)
    try:
        call = await simulate_call(client, prompt, scenario)
        run.transcript = call.transcript
        run.total_latency_ms = call.total_latency_ms
        run.mean_turn_latency_ms = call.mean_turn_latency_ms
        run.provider = call.providers[0] if call.providers else ""

        score, _ = await judge_transcript(
            client,
            scenario.invoice,
            call.transcript,
            assigned_tone=tone_for_tier(tier).value,
            expected_outcome=scenario.expected_outcome,
        )
        run.score = score

        outcome, _ = await extract_outcome(
            client,
            scenario.invoice,
            call.transcript,
            reference_date=EVAL_REFERENCE_DATE,
        )
        run.extracted_outcome = outcome.outcome
        run.promise_to_pay_date = outcome.promise_to_pay_date
    except Exception as exc:  # noqa: BLE001 - a failed variant must not kill the run
        logger.exception("Variant %s failed on %s", label, scenario.scenario_id)
        run.error = f"{type(exc).__name__}: {exc}"
    return run


async def _compare_scenario(
    client: LLMClient,
    scenario: EvalScenario,
    prompt_a: PromptVersion,
    prompt_b: PromptVersion,
    semaphore: asyncio.Semaphore,
    seed: int,
) -> ScenarioComparison:
    async with semaphore:
        run_a, run_b = await asyncio.gather(
            _run_variant(client, prompt_a, scenario, "A"),
            _run_variant(client, prompt_b, scenario, "B"),
        )

    # Deterministic per-scenario coin flip: reproducible across re-runs, but not the
    # same for every scenario, so any residual position bias averages out.
    present_a_first = random.Random(f"{seed}:{scenario.scenario_id}").random() < 0.5

    comparison = ScenarioComparison(
        scenario_id=scenario.scenario_id,
        is_adversarial=scenario.is_adversarial,
        expected_outcome=scenario.expected_outcome,
        a=run_a,
        b=run_b,
        presented_a_first=present_a_first,
    )

    if run_a.error or run_b.error:
        # A variant that crashed cannot be compared; the surviving one wins by default.
        comparison.pairwise_winner = "B" if run_a.error and not run_b.error else (
            "A" if run_b.error and not run_a.error else "tie"
        )
        comparison.pairwise_reason = "One or both variants errored."
        return comparison

    tier = classify_risk(scenario.invoice)
    try:
        async with semaphore:
            winner, verdict, _ = await judge_pairwise(
                client,
                scenario.invoice,
                run_a.transcript,
                run_b.transcript,
                assigned_tone=tone_for_tier(tier).value,
                expected_outcome=scenario.expected_outcome,
                present_a_first=present_a_first,
            )
        comparison.pairwise_winner = winner
        comparison.pairwise_reason = verdict.reason
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pairwise judgement failed for %s", scenario.scenario_id)
        comparison.pairwise_reason = f"pairwise judge failed: {exc}"

    return comparison


def _summarise(
    comparisons: list[ScenarioComparison],
    prompt: PromptVersion,
    label: str,
) -> VariantSummary:
    runs = [c.a if label == "A" else c.b for c in comparisons]
    ok = [r for r in runs if r.error is None and r.score is not None]

    summary = VariantSummary(prompt_id=prompt.id, label=label)
    summary.wins = sum(1 for c in comparisons if c.pairwise_winner == label)
    summary.errors = sum(1 for r in runs if r.error)

    if not ok:
        return summary

    summary.mean_composite = round(statistics.fmean(r.composite for r in ok), 3)
    latencies = [r.total_latency_ms for r in ok]
    summary.mean_latency_ms = round(statistics.fmean(latencies), 1)
    summary.p95_latency_ms = round(_percentile(latencies, 95), 1)
    summary.hallucination_rate = round(sum(r.hallucinated for r in ok) / len(ok), 3)
    summary.compliance_failures = sum(
        1 for r in ok if r.score and r.score.compliance_violations
    )

    matched = sum(
        1
        for r, c in zip(runs, comparisons, strict=True)
        if r.error is None and r.extracted_outcome == c.expected_outcome
    )
    summary.outcome_accuracy = round(matched / len(ok), 3)

    # Promise-date accuracy is only defined over scenarios that actually expect one.
    date_cases = [
        (r, c)
        for r, c in zip(runs, comparisons, strict=True)
        if r.error is None and c.expected_outcome is OutcomeType.PROMISE_TO_PAY
    ]
    if date_cases:
        summary.promise_date_accuracy = round(
            sum(1 for r, _ in date_cases if r.promise_to_pay_date is not None)
            / len(date_cases),
            3,
        )

    dims: dict[str, list[int]] = {}
    for r in ok:
        assert r.score is not None
        for dim, value in r.score.scores.items():
            dims.setdefault(dim.value, []).append(value)
    summary.dimension_means = {k: round(statistics.fmean(v), 2) for k, v in dims.items()}

    return summary


@dataclass
class ExperimentOutcome:
    comparisons: list[ScenarioComparison]
    summary_a: VariantSummary
    summary_b: VariantSummary
    ties: int
    win_rate_a: float
    win_rate_b: float
    adversarial_held_a: int
    adversarial_held_b: int


async def run_experiment(
    client: LLMClient,
    prompt_a: PromptVersion,
    prompt_b: PromptVersion,
    scenarios: list[EvalScenario],
    *,
    max_concurrency: int = 4,
    seed: int = 42,
    progress_cb=None,
) -> ExperimentOutcome:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def one(scenario: EvalScenario) -> ScenarioComparison:
        result = await _compare_scenario(
            client, scenario, prompt_a, prompt_b, semaphore, seed
        )
        if progress_cb:
            await progress_cb(result)
        return result

    comparisons = list(await asyncio.gather(*(one(s) for s in scenarios)))
    comparisons.sort(key=lambda c: c.scenario_id)

    summary_a = _summarise(comparisons, prompt_a, "A")
    summary_b = _summarise(comparisons, prompt_b, "B")
    ties = sum(1 for c in comparisons if c.pairwise_winner == "tie")

    total = len(comparisons) or 1
    # Ties are reported separately rather than split between variants: silently
    # awarding half a win to each inflates both win-rates toward 50%.
    adversarial = [c for c in comparisons if c.is_adversarial]

    return ExperimentOutcome(
        comparisons=comparisons,
        summary_a=summary_a,
        summary_b=summary_b,
        ties=ties,
        win_rate_a=round(summary_a.wins / total, 3),
        win_rate_b=round(summary_b.wins / total, 3),
        adversarial_held_a=sum(
            1 for c in adversarial if not c.a.hallucinated and c.a.error is None
        ),
        adversarial_held_b=sum(
            1 for c in adversarial if not c.b.hallucinated and c.b.error is None
        ),
    )
