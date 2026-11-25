"""The sweep summary and the CI quality gate built on it."""

from __future__ import annotations

from shared.eval.report import ScenarioRun, apply_gate, summarise


def run(sid, *, ok=True, tags=("t",), adversarial=False, held=None, halluc=False, date=None):
    return ScenarioRun(
        scenario_id=sid,
        tags=tuple(tags),
        expected="promise_to_pay",
        got="promise_to_pay" if ok else "unclear",
        outcome_correct=ok,
        date_correct=date,
        is_adversarial=adversarial,
        injection_held=held,
        composite=4.0,
        hallucinated=halluc,
    )


def test_summary_counts_and_interval() -> None:
    report = summarise([run("S1"), run("S2"), run("S3", ok=False), run("S4")])
    assert report.outcome_accuracy == 0.75
    lo, hi = report.outcome_accuracy_ci
    assert lo < 0.75 < hi  # four scenarios cannot pin accuracy down
    assert report.misses == ["S3"]


def test_by_tag_shows_where_it_fails() -> None:
    report = summarise(
        [run("S1", tags=["dispute"]), run("S2", ok=False, tags=["dispute", "usd"])]
    )
    assert report.by_tag["dispute"]["accuracy"] == 0.5
    assert report.by_tag["usd"]["accuracy"] == 0.0


def test_promise_dates_only_count_where_expected() -> None:
    report = summarise([run("S1", date=True), run("S2", date=False), run("S3")])
    assert report.promise_date_accuracy == 0.5


def test_passes_with_no_thresholds() -> None:
    assert apply_gate(summarise([run("S1", ok=False)])).passed


def test_accuracy_below_threshold_fails() -> None:
    report = apply_gate(summarise([run("S1"), run("S2", ok=False)]), min_accuracy=0.8)
    assert not report.passed
    assert "below 80.0%" in report.failures[0]


def test_unresisted_injection_fails() -> None:
    runs = [run("S1", adversarial=True, held=True), run("S2", adversarial=True, held=False)]
    report = apply_gate(summarise(runs), require_all_injections_resisted=True)
    assert report.failures == ["resisted 1 of 2 injection attempts"]


def test_hallucination_ceiling() -> None:
    report = apply_gate(
        summarise([run("S1", halluc=True), run("S2")]), max_hallucination_rate=0.1
    )
    assert not report.passed


def test_report_serialises_with_verdict() -> None:
    data = apply_gate(summarise([run("S1")]), min_accuracy=0.5).to_dict()
    assert data["passed"] is True
    assert data["by_tag"]["t"]["scenarios"] == 1
