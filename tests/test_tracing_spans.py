"""The spans the code emits: names, attributes, and parent/child structure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from shared.eval.judge import judge_transcript
from shared.models import CallOutcome, JudgeDimension, JudgeScore, OutcomeType
from tests.conftest import make_invoice
from voice_agent.outcome_extractor import extract_outcome


@dataclass
class FakeResponse:
    provider: str = "fake"
    model: str = "fake"
    latency_ms: float = 1.0
    text: str = ""


class FakeClient:
    def __init__(self, result) -> None:
        self.result = result

    async def complete_structured(self, messages, schema, **kwargs):
        return self.result, FakeResponse()


def by_name(spans, name):
    [span] = [s for s in spans.get_finished_spans() if s.name == name]
    return span


async def test_judge_span_carries_the_verdict(spans) -> None:
    score = JudgeScore(
        scores=dict.fromkeys(JudgeDimension, 4) | {JudgeDimension.COMPLIANT: 2},
        compliance_violations=["threatened legal action"],
    )
    await judge_transcript(FakeClient(score), make_invoice(), "agent: hello", assigned_tone="firm")
    span = by_name(spans, "eval.judge")
    assert span.attributes["eval.composite"] == score.composite
    assert span.attributes["eval.score.compliant"] == 2
    assert span.attributes["eval.compliance_violations"] == ("threatened legal action",)


async def test_extract_span_carries_the_outcome(spans) -> None:
    outcome = CallOutcome(
        outcome=OutcomeType.PROMISE_TO_PAY,
        promise_to_pay_date=date(2026, 8, 8),
        confidence=0.9,
    )
    await extract_outcome(FakeClient(outcome), make_invoice(), "customer: the eighth")
    span = by_name(spans, "eval.extract")
    assert span.attributes["call.outcome"] == "promise_to_pay"
    assert span.attributes["call.promise_to_pay_date"] == "2026-08-08"


def test_request_span_parents_the_work_it_triggers(spans, database_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    from fastapi.testclient import TestClient

    from ab_dashboard.main import app

    with TestClient(app) as client:
        client.post("/dunning/tick")
    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()

    request = by_name(spans, "HTTP POST /dunning/tick")
    tick = by_name(spans, "dunning.tick")
    assert tick.parent.span_id == request.context.span_id
    assert tick.context.trace_id == request.context.trace_id
    assert request.attributes["http.route"] == "/dunning/tick"
