"""Reading a finished experiment through the API: results and the scenario drill-down."""

from __future__ import annotations

import pytest

from ab_dashboard.db import ExperimentRow, ExperimentStatus, ResultRow
from shared.eval.eval_set import cached_eval_set


@pytest.fixture
def client(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    from fastapi.testclient import TestClient

    from ab_dashboard.main import app

    with TestClient(app) as c:
        yield c
    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()


def result(scenario_id: str, label: str, **overrides) -> ResultRow:
    base = dict(
        experiment_id="exp1",
        scenario_id=scenario_id,
        variant_label=label,
        prompt_id=f"prompt_{label.lower()}",
        transcript=f"agent: hello from {label}\ncustomer: ok",
        scores={"tone": 4, "accuracy": 5},
        composite=4.5 if label == "A" else 3.5,
        judge_rationale=f"{label} was clear",
        hallucinated=label == "B",
        hallucinated_figures=["Rs 99,999"] if label == "B" else [],
        compliance_violations=[],
        extracted_outcome="promise_to_pay",
        expected_outcome="promise_to_pay",
        outcome_match=True,
        promise_date_match=True,
        total_latency_ms=1200.0,
        mean_turn_latency_ms=300.0,
        provider="groq",
        tags=["promise"],
        pairwise_winner="A",
        pairwise_reason="A quoted the correct balance",
        presented_a_first=False,
    )
    return ResultRow(**{**base, **overrides})


@pytest.fixture
def finished(client):
    """A completed run over two real eval-set scenarios."""
    from ab_dashboard.db import get_sessionmaker

    first, second = (s.scenario_id for s in cached_eval_set()[:2])

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                ExperimentRow(
                    id="exp1",
                    variant_a_id="prompt_a",
                    variant_b_id="prompt_b",
                    status=ExperimentStatus.COMPLETED,
                    scenario_count=2,
                    completed_count=2,
                )
            )
            await session.flush()
            session.add_all([result(first, "A"), result(first, "B")])
            session.add_all(
                [
                    result(second, "A", pairwise_winner="B"),
                    result(second, "B", pairwise_winner="B"),
                ]
            )
            await session.commit()

    client.portal.call(seed)
    return first, second


def test_results_summarise_the_run(client, finished) -> None:
    body = client.get("/experiments/exp1/results").json()
    assert body["scenario_count"] == 2
    assert (body["variant_a"]["wins"], body["variant_b"]["wins"]) == (1, 1)
    assert body["variant_b"]["hallucination_rate"] == 1.0
    assert {s["scenario_id"] for s in body["per_scenario"]} == set(finished)


def test_scenario_detail_shows_both_transcripts_and_the_judges_reasons(client, finished) -> None:
    first, _ = finished
    body = client.get(f"/experiments/exp1/scenarios/{first}").json()
    scenario = next(s for s in cached_eval_set() if s.scenario_id == first)

    assert body["description"] == scenario.description
    assert body["customer_persona"] == scenario.customer_persona
    assert body["pairwise_winner"] == "A"
    assert body["pairwise_reason"] == "A quoted the correct balance"
    assert body["presented_a_first"] is False
    assert body["variant_a"]["transcript"].startswith("agent: hello from A")
    assert body["variant_a"]["judge_rationale"] == "A was clear"
    assert body["variant_b"]["hallucinated_figures"] == ["Rs 99,999"]
    assert body["variant_b"]["scores"] == {"tone": 4.0, "accuracy": 5.0}


def test_scenario_detail_404s(client, finished) -> None:
    assert client.get("/experiments/ghost/scenarios/x").status_code == 404
    assert client.get("/experiments/exp1/scenarios/not-a-scenario").status_code == 404


def test_scenario_removed_from_the_eval_set_still_shows_its_results(client) -> None:
    from ab_dashboard.db import get_sessionmaker

    async def seed():
        async with get_sessionmaker()() as session:
            session.add(
                ExperimentRow(
                    id="exp1",
                    variant_a_id="prompt_a",
                    variant_b_id="prompt_b",
                    status=ExperimentStatus.COMPLETED,
                )
            )
            await session.flush()
            session.add(result("retired_scenario", "A"))
            await session.commit()

    client.portal.call(seed)
    body = client.get("/experiments/exp1/scenarios/retired_scenario").json()
    assert body["description"] is None
    assert body["variant_a"]["prompt_id"] == "prompt_a"
    assert body["variant_b"] is None
