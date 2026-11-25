"""Eval set integrity and the experiment runner's aggregation logic."""

from __future__ import annotations

from datetime import date

import pytest

from ab_dashboard.experiment_runner import (
    ScenarioComparison,
    VariantRun,
    _percentile,
    _summarise,
)
from shared.eval.eval_set import load_eval_set
from shared.models import (
    JudgeDimension,
    JudgeScore,
    OutcomeType,
    PromptVersion,
    Tone,
)


class TestEvalSet:
    def test_loads_the_full_set(self) -> None:
        assert len(load_eval_set()) == 28

    def test_scenario_ids_are_unique(self) -> None:
        ids = [s.scenario_id for s in load_eval_set()]
        assert len(ids) == len(set(ids))

    def test_includes_adversarial_injection_cases(self) -> None:
        adversarial = [s for s in load_eval_set() if s.is_adversarial]
        assert len(adversarial) == 5
        assert all("adversarial" in s.tags for s in adversarial)

    def test_adversarial_cases_cover_distinct_attack_types(self) -> None:
        # Five copies of one attack would measure one guardrail five times.
        attacks = {
            "prompt_injection",
            "payment_fraud",
            "compliance_bait",
            "data_exfiltration",
        }
        seen = {t for s in load_eval_set() if s.is_adversarial for t in s.tags} & attacks
        assert seen == attacks

    def test_covers_more_than_one_foreign_currency(self) -> None:
        currencies = {s.invoice.currency for s in load_eval_set()}
        assert {"INR", "USD", "EUR", "GBP"} <= currencies

    def test_every_promise_date_is_after_the_reference_date(self) -> None:
        # A promise dated before the call cannot be a commitment; it would be a typo.
        from datetime import date

        for scenario in load_eval_set():
            if scenario.expected_promise_date:
                assert scenario.expected_promise_date > date(2026, 8, 1), scenario.scenario_id

    def test_covers_every_outcome_type_that_matters(self) -> None:
        seen = {s.expected_outcome for s in load_eval_set()}
        assert seen >= {
            OutcomeType.PROMISE_TO_PAY,
            OutcomeType.DISPUTE_RAISED,
            OutcomeType.ALREADY_PAID,
            OutcomeType.REFUSED,
            OutcomeType.NO_ANSWER,
            OutcomeType.UNCLEAR,
            OutcomeType.PARTIAL_PAYMENT,
        }

    def test_promise_scenarios_carry_a_ground_truth_date(self) -> None:
        """Without a date, promise-date accuracy cannot be measured for that scenario."""
        for scenario in load_eval_set():
            if scenario.expected_outcome is OutcomeType.PROMISE_TO_PAY:
                assert scenario.expected_promise_date, scenario.scenario_id

    def test_every_scenario_has_a_customer_script(self) -> None:
        for scenario in load_eval_set():
            assert scenario.customer_script, scenario.scenario_id

    def test_adversarial_scenarios_do_not_expect_a_commitment(self) -> None:
        """An injection scenario whose ground truth was promise_to_pay would reward the
        exact failure it exists to catch."""
        for scenario in load_eval_set():
            if scenario.is_adversarial:
                assert scenario.expected_outcome is not OutcomeType.PROMISE_TO_PAY

    def test_refusal_and_dispute_labels_match_the_extractor_rubric(self) -> None:
        """A scenario whose customer contests the invoice on quality/amount grounds is a
        dispute, not a refusal - regardless of whether they also withhold payment.

        Mislabelling these silently penalises a correct extractor, which is how the
        first full run scored 17/18 with a badly truncated agent.
        """
        by_id = {s.scenario_id: s for s in load_eval_set()}
        assert by_id["S07_refusal"].expected_outcome is OutcomeType.REFUSED
        assert by_id["S14_hostile"].expected_outcome is OutcomeType.REFUSED
        assert by_id["S17_injection_waive_invoice"].expected_outcome is OutcomeType.UNCLEAR

    def test_pure_refusal_scenarios_carry_no_invoice_complaint(self) -> None:
        """S07 must not mix a quality complaint into a refusal.

        When it did, the scenario read as either `refused` or `dispute_raised` depending
        on the run and flipped across three consecutive runs. Ambiguous ground truth is
        noise in the benchmark; genuine disputes are covered by S09 and S10.
        """
        s07 = next(s for s in load_eval_set() if s.scenario_id == "S07_refusal")
        script = " ".join(s07.customer_script).lower()
        for complaint in ("service level", "damaged", "not delivered", "wrong amount"):
            assert complaint not in script, f"S07 reintroduces a dispute signal: {complaint}"


def _prompt(pid: str) -> PromptVersion:
    return PromptVersion(id=pid, name="p", version=1, tone=Tone.FIRM, template="x")


def _run(
    label: str,
    *,
    scenario_id: str = "S1",
    composite: int = 4,
    outcome: OutcomeType | None = OutcomeType.PROMISE_TO_PAY,
    promise_date: date | None = date(2026, 8, 8),
    latency: float = 100.0,
    hallucinated: list[str] | None = None,
    violations: list[str] | None = None,
    error: str | None = None,
) -> VariantRun:
    score = (
        None
        if error
        else JudgeScore(
            scores=dict.fromkeys(JudgeDimension, composite),
            hallucinated_figures=hallucinated or [],
            compliance_violations=violations or [],
        )
    )
    return VariantRun(
        scenario_id=scenario_id,
        variant_label=label,
        prompt_id=f"prompt_{label}",
        transcript="agent: hi",
        score=score,
        extracted_outcome=outcome,
        promise_to_pay_date=promise_date,
        total_latency_ms=latency,
        error=error,
    )


def _comparison(
    a: VariantRun,
    b: VariantRun,
    *,
    scenario_id: str = "S1",
    winner: str = "tie",
    expected: OutcomeType = OutcomeType.PROMISE_TO_PAY,
    expected_date: date | None = None,
    adversarial: bool = False,
) -> ScenarioComparison:
    return ScenarioComparison(
        scenario_id=scenario_id,
        is_adversarial=adversarial,
        expected_outcome=expected,
        expected_promise_date=expected_date,
        a=a,
        b=b,
        pairwise_winner=winner,
    )


class TestSummarise:
    def test_counts_only_its_own_wins(self) -> None:
        comparisons = [
            _comparison(_run("A"), _run("B"), scenario_id="S1", winner="A"),
            _comparison(_run("A"), _run("B"), scenario_id="S2", winner="B"),
            _comparison(_run("A"), _run("B"), scenario_id="S3", winner="A"),
            _comparison(_run("A"), _run("B"), scenario_id="S4", winner="tie"),
        ]
        assert _summarise(comparisons, _prompt("a"), "A").wins == 2
        assert _summarise(comparisons, _prompt("b"), "B").wins == 1

    def test_ties_are_not_credited_to_either_variant(self) -> None:
        comparisons = [_comparison(_run("A"), _run("B"), winner="tie")]
        assert _summarise(comparisons, _prompt("a"), "A").wins == 0
        assert _summarise(comparisons, _prompt("b"), "B").wins == 0

    def test_outcome_accuracy_is_measured_against_ground_truth(self) -> None:
        comparisons = [
            _comparison(
                _run("A", outcome=OutcomeType.PROMISE_TO_PAY),
                _run("B", outcome=OutcomeType.REFUSED),
                expected=OutcomeType.PROMISE_TO_PAY,
            )
        ]
        assert _summarise(comparisons, _prompt("a"), "A").outcome_accuracy == 1.0
        assert _summarise(comparisons, _prompt("b"), "B").outcome_accuracy == 0.0

    def test_hallucination_rate(self) -> None:
        comparisons = [
            _comparison(_run("A", hallucinated=["7500"]), _run("B"), scenario_id="S1"),
            _comparison(_run("A"), _run("B"), scenario_id="S2"),
        ]
        assert _summarise(comparisons, _prompt("a"), "A").hallucination_rate == 0.5
        assert _summarise(comparisons, _prompt("b"), "B").hallucination_rate == 0.0

    def test_compliance_failures_counted(self) -> None:
        comparisons = [_comparison(_run("A", violations=["threatened legal action"]), _run("B"))]
        assert _summarise(comparisons, _prompt("a"), "A").compliance_failures == 1

    def test_promise_date_accuracy_only_covers_promise_scenarios(self) -> None:
        comparisons = [
            _comparison(
                _run("A", promise_date=date(2026, 8, 8)),
                _run("B", promise_date=None),
                scenario_id="S1",
                expected=OutcomeType.PROMISE_TO_PAY,
                expected_date=date(2026, 8, 8),
            ),
            _comparison(
                _run("A", outcome=OutcomeType.REFUSED, promise_date=None),
                _run("B", outcome=OutcomeType.REFUSED, promise_date=None),
                scenario_id="S2",
                expected=OutcomeType.REFUSED,
            ),
        ]
        # Denominator is 1 (only S1 expects a date), not 2.
        assert _summarise(comparisons, _prompt("a"), "A").promise_date_accuracy == 1.0
        assert _summarise(comparisons, _prompt("b"), "B").promise_date_accuracy == 0.0

    def test_wrong_promise_date_is_a_miss_not_a_hit(self) -> None:
        # Regression: this used to score as correct because *a* date was extracted.
        comparisons = [
            _comparison(
                _run("A", promise_date=date(2026, 8, 18)),
                _run("B", promise_date=date(2026, 8, 8)),
                expected_date=date(2026, 8, 8),
            )
        ]
        assert _summarise(comparisons, _prompt("a"), "A").promise_date_accuracy == 0.0
        assert _summarise(comparisons, _prompt("b"), "B").promise_date_accuracy == 1.0

    def test_promise_date_accuracy_is_none_when_no_scenario_expects_one(self) -> None:
        comparisons = [
            _comparison(
                _run("A", outcome=OutcomeType.REFUSED, promise_date=None),
                _run("B", outcome=OutcomeType.REFUSED, promise_date=None),
                expected=OutcomeType.REFUSED,
            )
        ]
        assert _summarise(comparisons, _prompt("a"), "A").promise_date_accuracy is None

    def test_errored_runs_are_counted_and_excluded_from_means(self) -> None:
        comparisons = [
            _comparison(_run("A", latency=100), _run("B"), scenario_id="S1"),
            _comparison(_run("A", error="boom"), _run("B"), scenario_id="S2"),
        ]
        summary = _summarise(comparisons, _prompt("a"), "A")
        assert summary.errors == 1
        assert summary.mean_latency_ms == 100.0

    def test_all_errored_returns_zeroed_summary_without_crashing(self) -> None:
        comparisons = [_comparison(_run("A", error="boom"), _run("B", error="boom"))]
        summary = _summarise(comparisons, _prompt("a"), "A")
        assert summary.errors == 1
        assert summary.mean_composite == 0.0

    def test_dimension_means_reported_per_dimension(self) -> None:
        comparisons = [_comparison(_run("A", composite=3), _run("B", composite=5))]
        means = _summarise(comparisons, _prompt("a"), "A").dimension_means
        assert set(means) == {d.value for d in JudgeDimension}
        assert all(v == 3.0 for v in means.values())


class TestPercentile:
    def test_empty(self) -> None:
        assert _percentile([], 95) == 0.0

    def test_single_value(self) -> None:
        assert _percentile([42.0], 95) == 42.0

    def test_p95_of_a_ramp(self) -> None:
        assert _percentile([float(i) for i in range(1, 101)], 95) == pytest.approx(96.0, abs=1.5)

    def test_p50_is_the_median(self) -> None:
        assert _percentile([1.0, 2.0, 3.0], 50) == 2.0
