"""Provider-agnostic LLM client with an explicit failover chain.

Groq -> Cerebras -> Ollama. All three speak the OpenAI wire protocol, so one
AsyncOpenAI client with a swapped base_url covers every hop.

Two things this buys us beyond convenience:
  * No single point of failure when a free tier rate-limits mid-experiment.
  * Every response records which provider served it, so an A/B result can never be
    silently confounded by a provider swap partway through a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from shared.config import Settings, get_settings
from shared.observability import metrics as m
from shared.observability.tracing import tracer

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class AllProvidersFailedError(RuntimeError):
    """Every provider in the chain was exhausted."""


class TokenBucket:
    """Client-side pacing against a tokens-per-minute quota.

    Backoff alone only reacts *after* a rejection, so every 429 is a wasted round-trip
    and the run's duration becomes a function of how badly it overshoots. This holds each
    request until the budget can actually cover it, so 429s stop happening at all.

    Cost is estimated as prompt + max_tokens because that is how the quota is charged:
    providers reserve the full max_tokens up front, not the tokens actually generated.
    That detail is why raising max_tokens to fix truncation silently tripled rate-limit
    pressure.

    One bucket per (provider, model) - Groq's limits are per-model.
    """

    def __init__(self, capacity: int, window_seconds: float = 60.0) -> None:
        self.capacity = capacity
        self.window = window_seconds
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._spent and now - self._spent[0][0] >= self.window:
            self._spent.popleft()

    @property
    def in_flight(self) -> int:
        return sum(tokens for _, tokens in self._spent)

    async def acquire(self, cost: int) -> float:
        """Block until `cost` fits in the window. Returns seconds waited."""
        # A single request larger than the whole window can never fit; let it through
        # and letthe server decide rather than deadlocking forever.
        cost = min(cost, self.capacity)
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                if self.in_flight + cost <= self.capacity:
                    self._spent.append((now, cost))
                    return waited
                oldest = self._spent[0][0]
                sleep_for = max(self.window - (now - oldest), 0.05)
            await asyncio.sleep(sleep_for)
            waited += sleep_for


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    # Rate limits are per-model on Groq, so routing structured work (judge, outcome
    # extraction) to a smaller model gives it its own token budget instead of competing
    # with dialogue generation for the same 8K/min.
    small_model: str | None = None
    # Cerebras' free tier caps context at 8K. A provider whose window can't hold the
    # request is skipped rather than attempted-and-failed.
    context_limit_tokens: int | None = None
    # `reasoning_effort` is a Groq/OpenAI parameter. Sending it to a provider that does
    # not accept it is a 400, so it is opt-in per provider rather than always-on.
    supports_reasoning_effort: bool = False
    # Per-model tokens-per-minute quota, used to pace requests client-side. 0 disables
    # pacing. Groq's free tier reports 8,000 via x-ratelimit-limit-tokens.
    tokens_per_minute: int = 0

    def model_for(self, use_small: bool) -> str:
        return (self.small_model or self.model) if use_small else self.model


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = ""
    attempts: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        """The model ran out of budget mid-answer.

        On reasoning models this is easy to hit without noticing: reasoning tokens are
        billed against the same max_tokens as the visible reply, so a generous-looking
        limit can leave nothing for the answer itself.
        """
        return self.finish_reason == "length" or not self.text.strip()


def estimate_tokens(text: str) -> int:
    """Cheap heuristic (~4 chars/token). Only used to decide provider eligibility,
    never for billing, so approximate is fine."""
    return len(text) // 4 + 1


def _is_retryable(exc: BaseException) -> bool:
    # Providers can say outright that retrying is futile. Groq sets
    # `x-should-retry: false` on a tokens-per-DAY exhaustion, where the only remedies are
    # waiting hours or switching model. Ignoring it costs five capped backoffs and a
    # misleading "all providers failed" instead of a clear "daily quota gone".
    if _explicitly_not_retryable(exc):
        return False
    if isinstance(exc, RateLimitError | APIConnectionError | httpx.TimeoutException):
        return True
    if isinstance(exc, APIStatusError):
        # 402 means the account has no quota at all. Retrying and then failing over is
        # pointless; it will still have no quota in eight seconds.
        if exc.status_code == 402:
            return False
        # Groq validates JSON server-side under response_format=json_object and returns
        # 400 json_validate_failed when the model emitted nothing usable - typically
        # because reasoning tokens consumed max_tokens. That is a transient generation
        # failure, not a malformed request, so it is worth another attempt. Without this
        # a single bad sample aborts an entire 18-scenario run.
        if exc.status_code == 400 and _is_json_validate_failure(exc):
            return True
        return exc.status_code in RETRYABLE_STATUS
    return False


def _explicitly_not_retryable(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return False
    return str(headers.get("x-should-retry", "")).strip().lower() == "false"


def _provider_message(exc: BaseException) -> str:
    """The provider's human-readable explanation, e.g. which quota was exhausted."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(exc)


def _error_code(exc: BaseException) -> str | None:
    """The provider's machine-readable error code, if it sent one."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
        if body.get("code"):
            return str(body["code"])
    return None


def _is_json_validate_failure(exc: APIStatusError) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "json_validate_failed":
            return True
    return "json_validate_failed" in str(exc)


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Honour the server's own backoff hint.

    Groq returns `retry-after` and `x-ratelimit-reset-tokens` on a 429. Its free tier
    refills tokens continuously, so the actual wait is often ~1s - blind exponential
    backoff sleeps far longer than needed, and a fixed cap can sleep far too little.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    # Deliberately NOT falling back to x-ratelimit-reset-requests: that is the *daily*
    # request window (it reads like "5h26m52.8s"). Using it to pace a per-minute token
    # 429 produces absurd waits that get clamped to the cap and then exhaust the retry
    # budget for no reason.
    for header in ("retry-after", "x-ratelimit-reset-tokens"):
        raw = headers.get(header)
        if not raw:
            continue
        try:
            return _parse_duration(str(raw))
        except ValueError:
            continue
    return None


def _parse_duration(raw: str) -> float:
    """Parse '1.012s', '2m59.56s', or a bare seconds count."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty duration")
    match = re.fullmatch(r"(?:(?P<h>[\d.]+)h)?(?:(?P<m>[\d.]+)m)?(?:(?P<s>[\d.]+)s)?", raw)
    if match and any(match.groupdict().values()):
        return (
            float(match.group("h") or 0) * 3600
            + float(match.group("m") or 0) * 60
            + float(match.group("s") or 0)
        )
    return float(raw)


def _extract_json(text: str) -> str:
    """Pull a JSON object out of a model response.

    Handles bare JSON, ```json fenced blocks, and reasoning models that emit prose
    before the object.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_retries_per_provider: int = 5,
        max_backoff_seconds: float = 30.0,
        timeout: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.max_retries_per_provider = max_retries_per_provider
        self.max_backoff_seconds = max_backoff_seconds
        self.timeout = timeout
        self._clients: dict[str, AsyncOpenAI] = {}
        self.providers = self._build_chain()
        self._buckets: dict[tuple[str, str], TokenBucket] = {}

    def _bucket(self, provider: Provider, model: str) -> TokenBucket | None:
        if not provider.tokens_per_minute:
            return None
        key = (provider.name, model)
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(provider.tokens_per_minute)
        return self._buckets[key]

    def _build_chain(self) -> list[Provider]:
        s = self.settings
        chain: list[Provider] = []
        if s.groq_api_key:
            chain.append(
                Provider(
                    "groq",
                    s.groq_base_url,
                    s.groq_api_key,
                    s.groq_model_large,
                    small_model=s.groq_model_small,
                    supports_reasoning_effort=True,
                    tokens_per_minute=s.groq_tokens_per_minute,
                )
            )
        if s.cerebras_api_key:
            chain.append(
                Provider(
                    "cerebras",
                    s.cerebras_base_url,
                    s.cerebras_api_key,
                    s.cerebras_model_large,
                    context_limit_tokens=s.cerebras_context_limit_tokens,
                )
            )
        # Ollama needs no key; it is only reachable if the user is running it.
        chain.append(Provider("ollama", s.ollama_base_url, "ollama", s.ollama_model))
        return chain

    def _client(self, provider: Provider) -> AsyncOpenAI:
        if provider.name not in self._clients:
            self._clients[provider.name] = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=self.timeout,
                max_retries=0,  # we drive retries ourselves so failover is explicit
            )
        return self._clients[provider.name]

    def _eligible(self, provider: Provider, prompt_tokens: int) -> bool:
        if provider.context_limit_tokens is None:
            return True
        # Leave headroom for the completion.
        return prompt_tokens + 1024 <= provider.context_limit_tokens

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        use_small: bool = False,
        reasoning_effort: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # One span per logical request, covering retries and failovers, with the
        # OpenTelemetry GenAI semantic-convention attributes so any tracing backend
        # can chart tokens and latency per model without custom parsing.
        with tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)
            span.set_attribute("gen_ai.request.temperature", temperature)
            try:
                result = await self._complete(
                    messages,
                    model=model,
                    use_small=use_small,
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except AllProvidersFailedError:
                m.LLM_EXHAUSTED.inc()
                span.set_attribute("error.type", "all_providers_failed")
                raise
            span.set_attribute("gen_ai.system", result.provider)
            span.set_attribute("gen_ai.response.model", result.model)
            span.set_attribute("gen_ai.usage.input_tokens", result.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", result.completion_tokens)
            span.set_attribute("gen_ai.usage.reasoning_tokens", result.reasoning_tokens)
            span.set_attribute("gen_ai.response.finish_reasons", [result.finish_reason])
            span.set_attribute("llm.attempts", result.attempts)
            return result

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
        use_small: bool,
        reasoning_effort: str | None,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        prompt_tokens = estimate_tokens(json.dumps(messages))
        attempts: list[str] = []
        last_exc: BaseException | None = None

        for provider in self.providers:
            if not self._eligible(provider, prompt_tokens):
                attempts.append(f"{provider.name}:skipped-context({prompt_tokens}tok)")
                logger.warning(
                    "Skipping %s: ~%d prompt tokens exceeds its %d limit",
                    provider.name,
                    prompt_tokens,
                    provider.context_limit_tokens,
                )
                continue

            chosen_model = model or provider.model_for(use_small)
            bucket = self._bucket(provider, chosen_model)
            # Reserve prompt + max_tokens, matching how the quota is actually charged.
            cost = prompt_tokens + max_tokens

            for attempt in range(1, self.max_retries_per_provider + 1):
                if bucket:
                    waited = await bucket.acquire(cost)
                    if waited > 0:
                        m.LLM_PACING_WAIT.labels(provider.name, chosen_model).inc(waited)
                    if waited > 0.5:
                        logger.info(
                            "Paced %s/%s: waited %.1fs for ~%d tokens of budget",
                            provider.name,
                            chosen_model,
                            waited,
                            cost,
                        )
                started = time.perf_counter()
                try:
                    kwargs: dict[str, Any] = {
                        "model": chosen_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    if response_format:
                        kwargs["response_format"] = response_format
                    if reasoning_effort and provider.supports_reasoning_effort:
                        kwargs["reasoning_effort"] = reasoning_effort
                    resp = await self._client(provider).chat.completions.create(**kwargs)
                    latency_ms = (time.perf_counter() - started) * 1000
                    attempts.append(f"{provider.name}:ok")
                    usage = resp.usage
                    choice = resp.choices[0]
                    details = getattr(usage, "completion_tokens_details", None)
                    result = LLMResponse(
                        text=choice.message.content or "",
                        provider=provider.name,
                        model=kwargs["model"],
                        latency_ms=latency_ms,
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
                        finish_reason=getattr(choice, "finish_reason", "") or "",
                        attempts=attempts,
                    )
                    m.LLM_REQUESTS.labels(provider.name, result.model, "ok").inc()
                    m.LLM_LATENCY.labels(provider.name, result.model).observe(latency_ms / 1000)
                    for kind, count in (
                        ("prompt", result.prompt_tokens),
                        ("completion", result.completion_tokens),
                        ("reasoning", result.reasoning_tokens),
                    ):
                        if count:
                            m.LLM_TOKENS.labels(provider.name, result.model, kind).inc(count)
                    if result.truncated:
                        m.LLM_TRUNCATED.labels(provider.name, result.model).inc()
                        logger.warning(
                            "%s returned a truncated reply (finish_reason=%r, "
                            "%d reasoning of %d completion tokens, max_tokens=%d). "
                            "Lower reasoning_effort or raise max_tokens.",
                            provider.name,
                            result.finish_reason,
                            result.reasoning_tokens,
                            result.completion_tokens,
                            max_tokens,
                        )
                    return result
                except Exception as exc:  # noqa: BLE001 - classified below
                    last_exc = exc
                    # Include the provider's own error code, not just the exception
                    # class. "BadRequestError" is unactionable;
                    # "BadRequestError/json_validate_failed" names the actual fault.
                    detail = type(exc).__name__
                    code = _error_code(exc)
                    if code:
                        detail = f"{detail}/{code}"
                    attempts.append(f"{provider.name}:attempt{attempt}:{detail}")
                    if not _is_retryable(exc):
                        m.LLM_REQUESTS.labels(provider.name, chosen_model, "failed").inc()
                        m.LLM_FAILOVERS.labels(provider.name).inc()
                        logger.warning(
                            "%s failed non-retryably: %s; failing over",
                            provider.name,
                            _provider_message(exc),
                        )
                        break
                    if attempt == self.max_retries_per_provider:
                        m.LLM_REQUESTS.labels(provider.name, chosen_model, "failed").inc()
                        m.LLM_FAILOVERS.labels(provider.name).inc()
                        logger.warning(
                            "%s exhausted %d retries; failing over",
                            provider.name,
                            self.max_retries_per_provider,
                        )
                        break
                    m.LLM_REQUESTS.labels(provider.name, chosen_model, "retry").inc()
                    # Prefer the server's own hint over guessing. Groq's free tier
                    # refills tokens continuously, so the real wait is often ~1s.
                    hinted = _retry_after_seconds(exc)
                    backoff = (
                        min(hinted + 0.25, self.max_backoff_seconds)
                        if hinted is not None
                        else min(2 ** (attempt - 1), self.max_backoff_seconds)
                    )
                    logger.info(
                        "%s attempt %d failed (%s); retrying in %.1fs%s",
                        provider.name,
                        attempt,
                        type(exc).__name__,
                        backoff,
                        " (server-hinted)" if hinted is not None else "",
                    )
                    await asyncio.sleep(backoff)

        raise AllProvidersFailedError(
            f"All providers failed. Attempts: {attempts}. "
            f"Last error: {_provider_message(last_exc) if last_exc else 'unknown'}"
        ) from last_exc

    async def complete_structured(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        model: str | None = None,
        use_small: bool = False,
        reasoning_effort: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        max_parse_retries: int = 2,
    ) -> tuple[T, LLMResponse]:
        """Complete and validate against a Pydantic schema.

        On a validation failure the error is fed back to the model, which recovers far
        more often than a blind retry does.
        """
        convo = list(messages)
        schema_hint = json.dumps(schema.model_json_schema(), indent=2)
        convo[-1] = {
            **convo[-1],
            "content": (
                f"{convo[-1]['content']}\n\n"
                f"Respond with ONLY a JSON object matching this schema:\n{schema_hint}"
            ),
        }

        last_error = ""
        for parse_attempt in range(max_parse_retries + 1):
            resp = await self.complete(
                convo,
                model=model,
                use_small=use_small,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            try:
                return schema.model_validate_json(_extract_json(resp.text)), resp
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                logger.warning(
                    "Structured parse failed (attempt %d): %s", parse_attempt + 1, last_error
                )
                if parse_attempt == max_parse_retries:
                    break
                convo = [
                    *convo,
                    {"role": "assistant", "content": resp.text},
                    {
                        "role": "user",
                        "content": (
                            f"That did not validate against the schema:\n{last_error}\n"
                            "Return corrected JSON only."
                        ),
                    },
                ]

        raise ValueError(f"Could not obtain valid {schema.__name__}: {last_error}")

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
