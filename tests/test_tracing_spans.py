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


class TestLangfuse:
    def test_off_without_keys(self, monkeypatch) -> None:
        from shared.observability.tracing import langfuse_exporter_config

        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        assert langfuse_exporter_config() is None

    def test_builds_otlp_endpoint_and_basic_auth(self, monkeypatch) -> None:
        import base64

        from shared.observability.tracing import langfuse_exporter_config

        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
        monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com/")
        config = langfuse_exporter_config()
        assert config["endpoint"] == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
        scheme, token = config["headers"]["Authorization"].split()
        assert scheme == "Basic"
        assert base64.b64decode(token).decode() == "pk-lf-1:sk-lf-2"

    def test_keys_alone_enable_tracing(self, monkeypatch) -> None:
        from opentelemetry import trace

        from shared.observability import tracing

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_TRACES_CONSOLE", raising=False)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.setattr(tracing, "_configured", False)
        monkeypatch.setattr(trace, "set_tracer_provider", lambda provider: None)
        assert tracing.setup_tracing("svc") is True


class TestMessageContent:
    async def _complete(self):
        from shared.llm_client import LLMClient
        from tests.test_llm_client import _install_fakes, _ok, _settings

        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(client, {"groq": [_ok("I will pay on the eighth")]})
        await client.complete([{"role": "user", "content": "INV-1 for Acme"}])

    async def test_not_captured_by_default(self, spans, monkeypatch) -> None:
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
        await self._complete()
        span = by_name(spans, "llm.complete")
        assert "gen_ai.prompt.0.content" not in span.attributes
        assert span.attributes["gen_ai.usage.input_tokens"] == 10

    async def test_captured_when_enabled(self, spans, monkeypatch) -> None:
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
        await self._complete()
        span = by_name(spans, "llm.complete")
        assert span.attributes["gen_ai.prompt.0.content"] == "INV-1 for Acme"
        assert span.attributes["gen_ai.completion.0.content"] == "I will pay on the eighth"


async def test_experiment_work_nests_under_its_run(spans, database_url, monkeypatch) -> None:
    # Regression: experiment.run was created but never made current, so every scenario
    # started an unrelated trace.
    import asyncio

    import ab_dashboard.db as db_module
    from ab_dashboard import jobs
    from shared.config import get_settings
    from shared.observability.tracing import tracer
    from tests.test_migrations_and_jobs import _NullClient, _seed_experiment

    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    await db_module.init_db()

    async def fake_run(*_a, **_k):
        with tracer.start_as_current_span("scenario.work"):
            pass

    monkeypatch.setattr(jobs, "run_experiment", fake_run)
    monkeypatch.setattr(jobs, "LLMClient", _NullClient)
    eid = await _seed_experiment(db_module, limit=1)
    assert await jobs.launch(eid)
    while eid in jobs._active:
        await asyncio.sleep(0.01)
    await db_module.dispose_engine()
    get_settings.cache_clear()

    run = by_name(spans, "experiment.run")
    work = by_name(spans, "scenario.work")
    assert work.parent.span_id == run.context.span_id
    assert run.attributes["langfuse.session.id"] == eid
