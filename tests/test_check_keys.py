"""The key checker: each service's success and failure paths, with no network."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from shared import check_keys as ck
from shared.config import Settings


def settings(**overrides) -> Settings:
    base = dict(
        groq_api_key=None,
        cerebras_api_key=None,
        deepgram_api_key=None,
        livekit_url=None,
        livekit_api_key=None,
        livekit_api_secret=None,
        voice_provider="cloud",
        database_url="sqlite+aiosqlite:///:memory:",
        ollama_base_url="http://ollama.test/v1",
    )
    return Settings(_env_file=None, **{**base, **overrides})


def transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Route by host + path; anything unrouted is a connection failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.host}{request.url.path}"
        if key not in routes:
            raise httpx.ConnectError("unreachable", request=request)
        return routes[key]

    return httpx.MockTransport(handler)


def by_name(results):
    return {r.name: r for r in results}


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)


async def test_unset_keys_are_skipped_not_failed():
    results = by_name(await ck.run_checks(settings(), transport=transport({})))
    assert results["Groq"].status == ck.SKIP
    assert results["Cerebras"].status == ck.SKIP
    assert results["Deepgram"].status == ck.SKIP
    assert results["LiveKit"].status == ck.SKIP
    assert results["Ollama"].status == ck.SKIP  # not running locally
    assert results["Database"].status == ck.OK


async def test_groq_ok_requires_every_configured_model_and_a_completion():
    s = settings(groq_api_key="gsk_test")
    models = {
        "data": [{"id": s.groq_model_small}, {"id": s.groq_model_large}, {"id": s.groq_stt_model}]
    }
    routes = {
        "GET api.groq.com/openai/v1/models": httpx.Response(200, json=models),
        "POST api.groq.com/openai/v1/chat/completions": httpx.Response(200, json={}),
    }
    result = by_name(await ck.run_checks(s, transport=transport(routes)))["Groq"]
    assert result.status == ck.OK


async def test_groq_missing_model_fails_with_its_name():
    s = settings(groq_api_key="gsk_test")
    routes = {
        "GET api.groq.com/openai/v1/models": httpx.Response(
            200, json={"data": [{"id": s.groq_model_small}]}
        )
    }
    result = by_name(await ck.run_checks(s, transport=transport(routes)))["Groq"]
    assert result.status == ck.FAIL
    assert s.groq_model_large in result.detail


async def test_invalid_key_reports_provider_message_without_the_key():
    s = settings(groq_api_key="gsk_secret_value")
    routes = {
        "GET api.groq.com/openai/v1/models": httpx.Response(
            401, json={"error": {"message": "Invalid API Key"}}
        )
    }
    result = by_name(await ck.run_checks(s, transport=transport(routes)))["Groq"]
    assert result.status == ck.FAIL
    assert "401" in result.detail and "Invalid API Key" in result.detail
    assert "gsk_secret_value" not in result.detail


async def test_cerebras_without_quota_fails_at_completion():
    s = settings(cerebras_api_key="csk_test")
    routes = {
        "GET api.cerebras.ai/v1/models": httpx.Response(
            200, json={"data": [{"id": s.cerebras_model_large}]}
        ),
        "POST api.cerebras.ai/v1/chat/completions": httpx.Response(
            402, json={"message": "Payment required"}
        ),
    }
    result = by_name(await ck.run_checks(s, transport=transport(routes)))["Cerebras"]
    assert result.status == ck.FAIL
    assert "402" in result.detail


async def test_deepgram_and_livekit_ok():
    s = settings(
        deepgram_api_key="dg",
        livekit_url="wss://demo.livekit.cloud",
        livekit_api_key="APIkey",
        livekit_api_secret="secret",
    )
    routes = {
        "GET api.deepgram.com/v1/projects": httpx.Response(200, json={"projects": [{}]}),
        "POST demo.livekit.cloud/twirp/livekit.RoomService/ListRooms": httpx.Response(
            200, json={"rooms": []}
        ),
    }
    results = by_name(await ck.run_checks(s, transport=transport(routes)))
    assert results["Deepgram"].status == ck.OK
    assert results["LiveKit"].status == ck.OK


async def test_livekit_partial_config_fails():
    s = settings(livekit_url="wss://demo.livekit.cloud")
    result = by_name(await ck.run_checks(s, transport=transport({})))["LiveKit"]
    assert result.status == ck.FAIL


async def test_unreachable_service_is_a_failure_not_a_crash():
    s = settings(deepgram_api_key="dg")
    result = by_name(await ck.run_checks(s, transport=transport({})))["Deepgram"]
    assert result.status == ck.FAIL


async def test_bad_database_url_fails():
    s = settings(database_url="sqlite+aiosqlite:////no/such/dir/x.db")
    result = by_name(await ck.run_checks(s, transport=transport({})))["Database"]
    assert result.status == ck.FAIL


def test_livekit_token_is_a_valid_hs256_jwt():
    token = ck.livekit_token("APIkey", "secret", now=1_000_000)
    header, payload, signature = token.split(".")
    expected = hmac.new(b"secret", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "==") == expected
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["iss"] == "APIkey"
    assert claims["video"] == {"roomList": True}
    assert claims["exp"] > 1_000_000


def test_render_lists_failures():
    text = ck.render(
        [ck.Result("Groq", ck.OK, "fine", ""), ck.Result("Cerebras", ck.FAIL, "402", "")]
    )
    assert "1 failed: Cerebras" in text
