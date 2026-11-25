"""Provider failover. Mocked - these must never hit a real API."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, RateLimitError
from pydantic import BaseModel

from shared.config import Settings
from shared.llm_client import (
    AllProvidersFailedError,
    LLMClient,
    _extract_json,
    _parse_duration,
    _retry_after_seconds,
    estimate_tokens,
)


def _settings(**overrides) -> Settings:
    base = {
        "groq_api_key": "test-groq",
        "cerebras_api_key": "test-cerebras",
        "cerebras_context_limit_tokens": 8000,
    }
    base.update(overrides)
    return Settings(**base)


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return RateLimitError("rate limited", response=response, body=None)


def _server_error() -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(503, request=request)
    return APIStatusError("unavailable", response=response, body=None)


def _ok(text: str = "hello"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, behaviour: list) -> None:
        self.behaviour = list(behaviour)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        item = self.behaviour.pop(0) if self.behaviour else _ok()
        if isinstance(item, Exception):
            raise item
        return item


def _install_fakes(client: LLMClient, per_provider: dict[str, list]) -> dict[str, FakeCompletions]:
    """Replace each provider's OpenAI client with a scripted fake."""
    fakes: dict[str, FakeCompletions] = {}
    for provider in client.providers:
        fake = FakeCompletions(per_provider.get(provider.name, []))
        fakes[provider.name] = fake
        client._clients[provider.name] = SimpleNamespace(
            chat=SimpleNamespace(completions=fake), close=_noop
        )
    return fakes


async def _noop():
    return None


class TestFailover:
    async def test_uses_groq_first_when_healthy(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        fakes = _install_fakes(client, {"groq": [_ok("from groq")]})
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "groq"
        assert resp.text == "from groq"
        assert fakes["cerebras"].calls == 0

    async def test_falls_over_to_cerebras_after_groq_exhausts_retries(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=2)
        fakes = _install_fakes(
            client,
            {
                "groq": [_rate_limit_error(), _rate_limit_error()],
                "cerebras": [_ok("from cerebras")],
            },
        )
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "cerebras"
        assert fakes["groq"].calls == 2, "should exhaust its retry budget before failing over"
        assert fakes["cerebras"].calls == 1

    async def test_retries_within_a_provider_before_moving_on(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=3)
        fakes = _install_fakes(client, {"groq": [_server_error(), _ok("recovered")]})
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "groq"
        assert resp.text == "recovered"
        assert fakes["groq"].calls == 2
        assert fakes["cerebras"].calls == 0

    async def test_non_retryable_error_fails_over_immediately(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=3)
        fakes = _install_fakes(
            client, {"groq": [ValueError("bad request")], "cerebras": [_ok("ok")]}
        )
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "cerebras"
        assert fakes["groq"].calls == 1, "must not burn retries on a non-retryable error"

    async def test_raises_when_every_provider_fails(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(
            client,
            {
                "groq": [_rate_limit_error()],
                "cerebras": [_rate_limit_error()],
                "ollama": [_rate_limit_error()],
            },
        )
        with pytest.raises(AllProvidersFailedError):
            await client.complete([{"role": "user", "content": "hi"}])

    async def test_records_the_attempt_chain(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(client, {"groq": [_rate_limit_error()], "cerebras": [_ok()]})
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert any("groq" in a for a in resp.attempts)
        assert resp.attempts[-1] == "cerebras:ok"


class TestContextLimit:
    async def test_skips_cerebras_when_prompt_exceeds_its_free_tier_window(self) -> None:
        """Cerebras' free tier caps context at 8K. Attempting a 30K-token prompt there
        would fail anyway; skipping is what lets the chain reach a provider that can."""
        client = LLMClient(_settings(), max_retries_per_provider=1)
        fakes = _install_fakes(
            client, {"groq": [_rate_limit_error()], "ollama": [_ok("from ollama")]}
        )
        huge = [{"role": "user", "content": "x" * 120_000}]
        resp = await client.complete(huge)
        assert fakes["cerebras"].calls == 0
        assert resp.provider == "ollama"
        assert any("skipped-context" in a for a in resp.attempts)

    async def test_uses_cerebras_when_the_prompt_fits(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        fakes = _install_fakes(client, {"groq": [_rate_limit_error()], "cerebras": [_ok()]})
        await client.complete([{"role": "user", "content": "short"}])
        assert fakes["cerebras"].calls == 1

    def test_token_estimate_scales_with_length(self) -> None:
        assert estimate_tokens("x" * 4000) > estimate_tokens("x" * 400)


def _status_error(code: int, headers: dict[str, str] | None = None) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(code, request=request, headers=headers or {})
    return APIStatusError("err", response=response, body=None)


class TestRetryAfterHandling:
    """Groq's free tier refills tokens continuously and reports the exact wait. Blind
    exponential backoff either oversleeps or gives up too early."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.012s", 1.012),
            ("2m59.56s", 179.56),
            ("12m57.599s", 777.599),
            ("1h2m3s", 3723.0),
            ("30", 30.0),
            ("0.5", 0.5),
        ],
    )
    def test_parses_groq_duration_formats(self, raw: str, expected: float) -> None:
        assert _parse_duration(raw) == pytest.approx(expected)

    def test_reads_retry_after_header(self) -> None:
        exc = _status_error(429, {"retry-after": "2.5"})
        assert _retry_after_seconds(exc) == pytest.approx(2.5)

    def test_falls_back_to_ratelimit_reset_tokens(self) -> None:
        exc = _status_error(429, {"x-ratelimit-reset-tokens": "1.012s"})
        assert _retry_after_seconds(exc) == pytest.approx(1.012)

    def test_returns_none_without_hint_headers(self) -> None:
        assert _retry_after_seconds(_status_error(429)) is None
        assert _retry_after_seconds(ValueError("no response")) is None

    def test_ignores_the_daily_request_window(self) -> None:
        """x-ratelimit-reset-requests is the DAILY window ('5h26m52.8s'). Pacing a
        per-minute token 429 off it produces an absurd wait that gets clamped to the cap
        and then burns the entire retry budget for nothing."""
        exc = _status_error(429, {"x-ratelimit-reset-requests": "5h26m52.8s"})
        assert _retry_after_seconds(exc) is None

    def test_prefers_token_reset_over_daily_window(self) -> None:
        exc = _status_error(
            429,
            {"x-ratelimit-reset-tokens": "1.2s", "x-ratelimit-reset-requests": "5h26m"},
        )
        assert _retry_after_seconds(exc) == pytest.approx(1.2)

    async def test_waits_the_hinted_duration_rather_than_guessing(self, monkeypatch) -> None:
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("shared.llm_client.asyncio.sleep", fake_sleep)

        client = LLMClient(_settings(), max_retries_per_provider=3)
        _install_fakes(
            client,
            {"groq": [_status_error(429, {"retry-after": "1.0"}), _ok("recovered")]},
        )
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.text == "recovered"
        assert slept == [pytest.approx(1.25)], "should use the hint, not 2**n"

    async def test_backoff_is_capped(self, monkeypatch) -> None:
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("shared.llm_client.asyncio.sleep", fake_sleep)

        client = LLMClient(
            _settings(), max_retries_per_provider=3, max_backoff_seconds=5.0
        )
        _install_fakes(
            client,
            {"groq": [_status_error(429, {"retry-after": "600"}), _ok("ok")]},
        )
        await client.complete([{"role": "user", "content": "hi"}])
        assert slept == [5.0], "a 10-minute hint must not stall the whole run"


class TestTokenBucket:
    """Proactive pacing. Backoff only reacts after a rejection; this stops the request
    being made until the quota can actually cover it."""

    async def test_allows_requests_that_fit(self) -> None:
        from shared.llm_client import TokenBucket

        bucket = TokenBucket(capacity=8000)
        assert await bucket.acquire(3000) == 0.0
        assert await bucket.acquire(4000) == 0.0
        assert bucket.in_flight == 7000

    async def test_blocks_once_the_window_is_full(self, monkeypatch) -> None:
        from shared.llm_client import TokenBucket

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            # Simulate the window advancing so the loop terminates.
            bucket._spent.clear()

        bucket = TokenBucket(capacity=8000, window_seconds=60.0)
        monkeypatch.setattr("shared.llm_client.asyncio.sleep", fake_sleep)

        await bucket.acquire(7000)
        waited = await bucket.acquire(3000)
        assert slept, "second request should have been held"
        assert waited > 0

    async def test_oversized_request_is_not_a_deadlock(self) -> None:
        """A single call larger than the whole window must still go out - clamped, and
        left for the server to reject, rather than looping forever."""
        from shared.llm_client import TokenBucket

        bucket = TokenBucket(capacity=8000)
        assert await bucket.acquire(50_000) == 0.0

    async def test_cost_reservation_reaches_the_bucket(self) -> None:
        """The quota is charged on reserved max_tokens, not tokens generated - so the
        reservation must include max_tokens or pacing under-counts badly."""
        client = LLMClient(_settings())
        _install_fakes(client, {"groq": [_ok()]})
        await client.complete(
            [{"role": "user", "content": "hi"}], max_tokens=1000, use_small=False
        )
        bucket = client._buckets[("groq", client.settings.groq_model_large)]
        assert bucket.in_flight >= 1000

    async def test_separate_buckets_per_model(self) -> None:
        """Groq's limits are per-model, so the small model must not consume the large
        model's budget."""
        client = LLMClient(_settings())
        _install_fakes(client, {"groq": [_ok(), _ok()]})
        await client.complete([{"role": "user", "content": "hi"}], max_tokens=100)
        await client.complete(
            [{"role": "user", "content": "hi"}], max_tokens=100, use_small=True
        )
        keys = {k[1] for k in client._buckets}
        assert keys == {
            client.settings.groq_model_large,
            client.settings.groq_model_small,
        }

    def test_pacing_is_off_for_providers_without_a_declared_quota(self) -> None:
        client = LLMClient(_settings())
        cerebras = next(p for p in client.providers if p.name == "cerebras")
        assert client._bucket(cerebras, cerebras.model) is None


class TestJsonValidateFailure:
    """Groq validates JSON server-side. When reasoning tokens starve the schema it
    returns 400/json_validate_failed with an empty generation - a transient generation
    fault, not a malformed request. Treating it as fatal aborted an entire 18-scenario
    run on one bad sample."""

    def _json_fail(self) -> APIStatusError:
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(400, request=request)
        return APIStatusError(
            "err",
            response=response,
            body={
                "error": {
                    "message": "Failed to validate JSON.",
                    "code": "json_validate_failed",
                    "failed_generation": "",
                }
            },
        )

    async def test_is_retried_rather_than_failing_the_run(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=3)
        fakes = _install_fakes(client, {"groq": [self._json_fail(), _ok("recovered")]})
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.text == "recovered"
        assert resp.provider == "groq"
        assert fakes["groq"].calls == 2

    async def test_a_plain_400_still_fails_over_without_retrying(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=3)
        fakes = _install_fakes(
            client, {"groq": [_status_error(400)], "cerebras": [_ok("ok")]}
        )
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "cerebras"
        assert fakes["groq"].calls == 1

    async def test_attempt_log_records_the_provider_error_code(self) -> None:
        """'BadRequestError' alone is unactionable in a log."""
        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(client, {"groq": [self._json_fail()], "cerebras": [_ok()]})
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert any("json_validate_failed" in a for a in resp.attempts)


class TestReasoningEffort:
    async def test_forwarded_to_providers_that_support_it(self) -> None:
        client = LLMClient(_settings())
        captured: dict[str, object] = {}

        class Capturing(FakeCompletions):
            async def create(self, **kwargs):
                captured.update(kwargs)
                return await super().create(**kwargs)

        client._clients["groq"] = SimpleNamespace(
            chat=SimpleNamespace(completions=Capturing([_ok()])), close=_noop
        )
        await client.complete([{"role": "user", "content": "hi"}], reasoning_effort="low")
        assert captured["reasoning_effort"] == "low"

    async def test_withheld_from_providers_that_would_reject_it(self) -> None:
        """Cerebras does not accept reasoning_effort; sending it would be a 400."""
        client = LLMClient(_settings())
        captured: dict[str, object] = {}

        class Capturing(FakeCompletions):
            async def create(self, **kwargs):
                captured.update(kwargs)
                return await super().create(**kwargs)

        client._clients["cerebras"] = SimpleNamespace(
            chat=SimpleNamespace(completions=Capturing([_ok()])), close=_noop
        )
        client.providers = [p for p in client.providers if p.name == "cerebras"]
        await client.complete([{"role": "user", "content": "hi"}], reasoning_effort="low")
        assert "reasoning_effort" not in captured

    def test_truncation_is_detected_from_finish_reason(self) -> None:
        from shared.llm_client import LLMResponse

        assert LLMResponse(text="cut off", provider="g", model="m", latency_ms=1,
                           finish_reason="length").truncated
        assert LLMResponse(text="", provider="g", model="m", latency_ms=1,
                           finish_reason="stop").truncated
        assert not LLMResponse(text="fine", provider="g", model="m", latency_ms=1,
                               finish_reason="stop").truncated


class TestPaymentRequired:
    async def test_402_fails_over_immediately_without_retrying(self) -> None:
        """A 402 means the account has no quota. It will still have no quota in eight
        seconds, so retrying only delays reaching a provider that works."""
        client = LLMClient(_settings(), max_retries_per_provider=5)
        fakes = _install_fakes(
            client, {"cerebras": [_status_error(402)], "groq": [_status_error(402)],
                     "ollama": [_ok("from ollama")]}
        )
        resp = await client.complete([{"role": "user", "content": "hi"}])
        assert resp.provider == "ollama"
        assert fakes["groq"].calls == 1
        assert fakes["cerebras"].calls == 1


class TestSmallModelRouting:
    """Rate limits are per-model, so structured work gets its own token budget."""

    async def test_use_small_selects_the_small_model(self) -> None:
        client = LLMClient(_settings())
        captured: dict[str, str] = {}

        class Capturing(FakeCompletions):
            async def create(self, **kwargs):
                captured["model"] = kwargs["model"]
                return await super().create(**kwargs)

        fake = Capturing([_ok()])
        client._clients["groq"] = SimpleNamespace(
            chat=SimpleNamespace(completions=fake), close=_noop
        )
        await client.complete([{"role": "user", "content": "hi"}], use_small=True)
        assert captured["model"] == client.settings.groq_model_small

    async def test_default_uses_the_large_model(self) -> None:
        client = LLMClient(_settings())
        captured: dict[str, str] = {}

        class Capturing(FakeCompletions):
            async def create(self, **kwargs):
                captured["model"] = kwargs["model"]
                return await super().create(**kwargs)

        fake = Capturing([_ok()])
        client._clients["groq"] = SimpleNamespace(
            chat=SimpleNamespace(completions=fake), close=_noop
        )
        await client.complete([{"role": "user", "content": "hi"}])
        assert captured["model"] == client.settings.groq_model_large

    def test_provider_without_a_small_model_falls_back_to_large(self) -> None:
        client = LLMClient(_settings())
        cerebras = next(p for p in client.providers if p.name == "cerebras")
        assert cerebras.small_model is None
        assert cerebras.model_for(use_small=True) == cerebras.model


class Shape(BaseModel):
    name: str
    count: int


class TestStructuredOutput:
    async def test_parses_a_valid_object(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(client, {"groq": [_ok(json.dumps({"name": "a", "count": 2}))]})
        parsed, _ = await client.complete_structured(
            [{"role": "user", "content": "go"}], Shape
        )
        assert parsed == Shape(name="a", count=2)

    async def test_feeds_validation_errors_back_and_recovers(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        fakes = _install_fakes(
            client,
            {"groq": [_ok('{"name": "a"}'), _ok('{"name": "a", "count": 3}')]},
        )
        parsed, _ = await client.complete_structured(
            [{"role": "user", "content": "go"}], Shape, max_parse_retries=2
        )
        assert parsed.count == 3
        assert fakes["groq"].calls == 2

    async def test_gives_up_after_max_parse_retries(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        _install_fakes(client, {"groq": [_ok("not json")] * 5})
        with pytest.raises(ValueError, match="Could not obtain valid Shape"):
            await client.complete_structured(
                [{"role": "user", "content": "go"}], Shape, max_parse_retries=1
            )


class TestJsonExtraction:
    def test_bare_object(self) -> None:
        assert json.loads(_extract_json('{"a": 1}')) == {"a": 1}

    def test_fenced_block(self) -> None:
        assert json.loads(_extract_json('```json\n{"a": 1}\n```')) == {"a": 1}

    def test_reasoning_preamble_before_object(self) -> None:
        text = 'Let me think about this.\n{"a": 1, "b": {"c": 2}}\nDone.'
        assert json.loads(_extract_json(text)) == {"a": 1, "b": {"c": 2}}

    def test_braces_inside_strings_do_not_terminate_early(self) -> None:
        text = '{"note": "a } brace", "n": 1}'
        assert json.loads(_extract_json(text)) == {"note": "a } brace", "n": 1}

    def test_escaped_quote_inside_string(self) -> None:
        text = r'{"note": "say \"hi\"", "n": 1}'
        assert json.loads(_extract_json(text))["n"] == 1



class TestMetrics:
    """Counters are read before and after, since the registry is process-global."""

    @staticmethod
    def value(name: str, **labels) -> float:
        from shared.observability.metrics import REGISTRY

        return REGISTRY.get_sample_value(name, labels) or 0.0

    async def test_success_records_request_and_tokens(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        model = client.providers[0].model
        ok = {"provider": "groq", "model": model, "result": "ok"}
        prompt = {"provider": "groq", "model": model, "kind": "prompt"}
        before_ok, before_tokens = self.value("llm_requests_total", **ok), self.value(
            "llm_tokens_total", **prompt
        )
        _install_fakes(client, {"groq": [_ok()]})

        await client.complete([{"role": "user", "content": "hi"}])

        assert self.value("llm_requests_total", **ok) == before_ok + 1
        assert self.value("llm_tokens_total", **prompt) == before_tokens + 10

    async def test_failover_and_retry_are_counted(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=2)
        before_failover = self.value("llm_failovers_total", from_provider="groq")
        _install_fakes(
            client,
            {"groq": [_rate_limit_error(), _rate_limit_error()], "cerebras": [_ok()]},
        )
        client.max_backoff_seconds = 0

        await client.complete([{"role": "user", "content": "hi"}])

        assert self.value("llm_failovers_total", from_provider="groq") == before_failover + 1

    async def test_total_failure_is_counted(self) -> None:
        client = LLMClient(_settings(), max_retries_per_provider=1)
        before = self.value("llm_all_providers_failed_total")
        _install_fakes(
            client,
            {name: [_server_error()] for name in ("groq", "cerebras", "ollama")},
        )

        with pytest.raises(AllProvidersFailedError):
            await client.complete([{"role": "user", "content": "hi"}])

        assert self.value("llm_all_providers_failed_total") == before + 1
