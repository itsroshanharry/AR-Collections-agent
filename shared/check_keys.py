"""Check that every configured API key and service actually works.

    uv run python -m shared.check_keys

Each check makes the smallest real request that proves the credential: list models and
run a one-line completion on the configured model, list Deepgram projects, list LiveKit
rooms, and so on. A key that is not set is reported as skipped, not failed: most are
optional. The exit code is non-zero only if a configured service fails, so the same
command works as a pre-deploy check.

Key values are never printed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from shared.config import Settings, get_settings

OK, FAIL, SKIP = "ok", "fail", "skip"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@dataclass
class Result:
    name: str
    status: str
    detail: str
    used_for: str


def _error(resp: httpx.Response) -> str:
    """The provider's own error message, short. Never echoes request headers."""
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        message = err.get("message") if isinstance(err, dict) else err
        message = message or body.get("err_msg") or body.get("msg") or body
    except ValueError:
        message = resp.text
    return f"HTTP {resp.status_code}: {str(message)[:160]}"


async def _openai_compatible(
    client: httpx.AsyncClient, base_url: str, key: str, models: list[str]
) -> tuple[str, str]:
    """List models, confirm the configured ones exist, then run a tiny completion."""
    headers = {"Authorization": f"Bearer {key}"}
    resp = await client.get(f"{base_url}/models", headers=headers)
    if resp.status_code != 200:
        return FAIL, _error(resp)
    available = {m["id"] for m in resp.json().get("data", [])}
    missing = [m for m in models if m not in available]
    if missing:
        return FAIL, f"key works, but model(s) not available: {', '.join(missing)}"
    started = time.perf_counter()
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={
            "model": models[0],
            "messages": [{"role": "user", "content": "Reply with the word ok."}],
            "max_tokens": 64,
        },
    )
    if resp.status_code != 200:
        return FAIL, f"models listed, but completion failed: {_error(resp)}"
    ms = (time.perf_counter() - started) * 1000
    return OK, f"{len(models)} model(s) available; completion on {models[0]} in {ms:.0f} ms"


async def check_groq(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "LLM (primary), speech-to-text in local voice mode"
    if not s.groq_api_key:
        return Result("Groq", SKIP, "GROQ_API_KEY not set", used_for)
    models = [s.groq_model_small, s.groq_model_large, s.groq_stt_model]
    status, detail = await _openai_compatible(client, s.groq_base_url, s.groq_api_key, models)
    return Result("Groq", status, detail, used_for)


async def check_cerebras(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "LLM (first fallback)"
    if not s.cerebras_api_key:
        return Result("Cerebras", SKIP, "CEREBRAS_API_KEY not set", used_for)
    status, detail = await _openai_compatible(
        client, s.cerebras_base_url, s.cerebras_api_key, [s.cerebras_model_large]
    )
    return Result("Cerebras", status, detail, used_for)


async def check_ollama(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "LLM (last-resort local fallback, optional)"
    try:
        resp = await client.get(f"{s.ollama_base_url}/models", timeout=3.0)
    except httpx.HTTPError:
        return Result("Ollama", SKIP, f"not running at {s.ollama_base_url}", used_for)
    if resp.status_code != 200:
        return Result("Ollama", FAIL, _error(resp), used_for)
    names = {m["id"] for m in resp.json().get("data", [])}
    if s.ollama_model not in names:
        return Result("Ollama", FAIL, f"running, but {s.ollama_model} is not pulled", used_for)
    return Result("Ollama", OK, f"running with {s.ollama_model}", used_for)


async def check_deepgram(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "speech-to-text and text-to-speech in cloud voice mode"
    if not s.deepgram_api_key:
        return Result("Deepgram", SKIP, "DEEPGRAM_API_KEY not set", used_for)
    resp = await client.get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {s.deepgram_api_key}"},
    )
    if resp.status_code != 200:
        return Result("Deepgram", FAIL, _error(resp), used_for)
    projects = resp.json().get("projects", [])
    return Result("Deepgram", OK, f"key valid ({len(projects)} project(s))", used_for)


def livekit_token(api_key: str, api_secret: str, *, now: float | None = None) -> str:
    """A short-lived LiveKit access token allowed to list rooms (HS256 JWT)."""
    now = int(now if now is not None else time.time())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "iss": api_key,
        "sub": "check-keys",
        "nbf": now - 10,
        "exp": now + 60,
        "video": {"roomList": True},
    }
    payload = b64(json.dumps(claims).encode())
    signature = hmac.new(api_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256)
    return f"{header}.{payload}.{b64(signature.digest())}"


async def check_livekit(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "voice calls in room/browser mode (console mode needs none)"
    values = (s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    if not any(values):
        return Result("LiveKit", SKIP, "LIVEKIT_* not set", used_for)
    if not all(values):
        return Result(
            "LiveKit", FAIL, "set all three: LIVEKIT_URL, _API_KEY, _API_SECRET", used_for
        )
    base = s.livekit_url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
    resp = await client.post(
        f"{base}/twirp/livekit.RoomService/ListRooms",
        headers={
            "Authorization": f"Bearer {livekit_token(s.livekit_api_key, s.livekit_api_secret)}"
        },
        json={},
    )
    if resp.status_code != 200:
        return Result("LiveKit", FAIL, _error(resp), used_for)
    rooms = resp.json().get("rooms", [])
    return Result("LiveKit", OK, f"key and secret valid ({len(rooms)} active room(s))", used_for)


async def check_kokoro(client: httpx.AsyncClient, s: Settings) -> Result:
    used_for = "text-to-speech in local voice mode (optional)"
    if s.voice_provider != "local":
        return Result("Kokoro", SKIP, "VOICE_PROVIDER is cloud", used_for)
    try:
        resp = await client.get(f"{s.kokoro_base_url}/models", timeout=3.0)
    except httpx.HTTPError:
        return Result("Kokoro", SKIP, f"not running at {s.kokoro_base_url}", used_for)
    status = OK if resp.status_code < 500 else FAIL
    return Result("Kokoro", status, f"reachable (HTTP {resp.status_code})", used_for)


async def check_langfuse(client: httpx.AsyncClient, s: Settings) -> Result:
    import os

    used_for = "LLM traces (optional)"
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not public and not secret:
        return Result("Langfuse", SKIP, "LANGFUSE_* not set", used_for)
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    resp = await client.get(f"{host}/api/public/projects", auth=(public or "", secret or ""))
    if resp.status_code != 200:
        return Result("Langfuse", FAIL, _error(resp), used_for)
    return Result("Langfuse", OK, f"keys valid for {host}", used_for)


async def check_database(s: Settings) -> Result:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ab_dashboard.db import normalise_database_url

    used_for = "everything: workflow state, tasks, experiments"
    # Check the URL the app would actually use: Neon hands out `postgresql://`, which
    # the app rewrites to the async psycopg driver.
    url = normalise_database_url(s.database_url)
    kind = url.split(":", 1)[0]
    engine = None
    try:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        return Result("Database", FAIL, f"{kind}: {type(exc).__name__}: {str(exc)[:120]}", used_for)
    finally:
        if engine is not None:
            await engine.dispose()
    return Result("Database", OK, f"{kind} connection works", used_for)


HTTP_CHECKS: list[Callable[[httpx.AsyncClient, Settings], Awaitable[Result]]] = [
    check_groq,
    check_cerebras,
    check_ollama,
    check_deepgram,
    check_livekit,
    check_kokoro,
    check_langfuse,
]


async def run_checks(settings: Settings | None = None, *, transport=None) -> list[Result]:
    s = settings or get_settings()
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:

        async def guarded(check) -> Result:
            try:
                return await check(client, s)
            except httpx.HTTPError as exc:
                name = check.__name__.removeprefix("check_").capitalize()
                return Result(name, FAIL, f"{type(exc).__name__}: {exc}"[:160], "")

        results = list(await asyncio.gather(*(guarded(c) for c in HTTP_CHECKS)))
    results.append(await check_database(s))
    return results


def render(results: list[Result]) -> str:
    marks = {OK: "OK  ", FAIL: "FAIL", SKIP: "skip"}
    width = max(len(r.name) for r in results)
    lines = [f"  {marks[r.status]}  {r.name:<{width}}  {r.detail}" for r in results]
    failed = [r.name for r in results if r.status == FAIL]
    lines.append("")
    lines.append(
        f"  {len(failed)} failed: {', '.join(failed)}"
        if failed
        else "  All configured services work."
    )
    return "\n".join(lines)


def main() -> int:
    # psycopg's async mode cannot run on Windows' default Proactor loop, so a Postgres
    # DATABASE_URL would always fail here on Windows without this.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    results = asyncio.run(run_checks(), loop_factory=loop_factory)
    print(render(results))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
