"""Text-only call simulation over the same prompt -> LLM -> extractor -> judge path.

This is what makes the eval set runnable at all. Driving a real audio session for 18
scenarios x 2 prompt variants would be slow, non-deterministic, and impossible in CI.
The simulator swaps only the transport: the agent's system prompt, the model, the
outcome extractor and the judge are byte-identical to the live voice path, so a
dashboard result is a statement about the real agent, not a proxy for it.

The customer side is a fixed script rather than a second LLM. That is deliberate - a
scripted counterparty makes the benchmark constant across prompt versions, which is the
entire point of an A/B eval set.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from shared.llm_client import LLMClient
from shared.models import EvalScenario, PromptVersion

logger = logging.getLogger(__name__)

# Generous relative to a one-or-two-sentence reply, because on a reasoning model the
# reasoning is billed against this same budget. At 160 with default effort, ~97 tokens
# went to reasoning and replies were being truncated mid-sentence.
MAX_REPLY_TOKENS = 400

# Dialogue turns think as little as possible. Two reasons: a truncated reply is worse
# than a slightly less considered one, and on the live voice path every reasoning token
# is silence the caller hears before the agent starts speaking.
DIALOGUE_REASONING_EFFORT = "low"


class EmptyReplyError(RuntimeError):
    """The model produced no visible text - usually reasoning consuming max_tokens."""


@dataclass
class SimulatedCall:
    scenario_id: str
    prompt_id: str
    turns: list[tuple[str, str]] = field(default_factory=list)
    total_latency_ms: float = 0.0
    turn_latencies_ms: list[float] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    reasoning_tokens: int = 0

    @property
    def transcript(self) -> str:
        return "\n".join(f"{speaker}: {text}" for speaker, text in self.turns)

    @property
    def agent_turns(self) -> list[str]:
        return [t for s, t in self.turns if s == "agent"]

    @property
    def mean_turn_latency_ms(self) -> float:
        if not self.turn_latencies_ms:
            return 0.0
        return sum(self.turn_latencies_ms) / len(self.turn_latencies_ms)


async def simulate_call(
    client: LLMClient,
    prompt: PromptVersion,
    scenario: EvalScenario,
    *,
    max_reply_tokens: int = MAX_REPLY_TOKENS,
) -> SimulatedCall:
    """Run one scripted call and return the transcript with per-turn timings.

    Turn order: the agent opens, then each scripted customer line gets exactly one
    agent reply. Latency is measured per agent turn, since that is what a caller
    actually waits on.
    """
    system_prompt = prompt.render(scenario.invoice)
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    call = SimulatedCall(scenario_id=scenario.scenario_id, prompt_id=prompt.id)

    started = time.perf_counter()

    async def agent_turn() -> None:
        resp = await client.complete(
            messages,
            temperature=0.7,
            max_tokens=max_reply_tokens,
            reasoning_effort=DIALOGUE_REASONING_EFFORT,
        )
        text = resp.text.strip()
        if not text:
            # An empty turn silently corrupts everything downstream: the judge scores a
            # blank, the extractor sees a gap, and the transcript reads as if the agent
            # said nothing. Fail loudly instead.
            raise EmptyReplyError(
                f"{resp.provider}/{resp.model} returned an empty reply for "
                f"{scenario.scenario_id} (finish_reason={resp.finish_reason!r}, "
                f"{resp.reasoning_tokens} reasoning tokens of "
                f"{resp.completion_tokens} completion)"
            )
        if resp.truncated:
            logger.warning(
                "Truncated agent turn in %s: %r", scenario.scenario_id, text[-60:]
            )
        call.turns.append(("agent", text))
        call.turn_latencies_ms.append(resp.latency_ms)
        call.providers.append(resp.provider)
        call.reasoning_tokens += resp.reasoning_tokens
        messages.append({"role": "assistant", "content": text})

    # Opening turn.
    messages.append({"role": "user", "content": "[The customer has picked up the phone.]"})
    await agent_turn()

    for line in scenario.customer_script:
        call.turns.append(("customer", line))
        messages.append({"role": "user", "content": line})
        await agent_turn()

    call.total_latency_ms = (time.perf_counter() - started) * 1000

    if len(set(call.providers)) > 1:
        # Surfaced rather than swallowed: a mid-call failover changes the model, which
        # would silently confound any A/B comparison built on this transcript.
        logger.warning(
            "Scenario %s / prompt %s served by mixed providers: %s",
            scenario.scenario_id,
            prompt.id,
            call.providers,
        )

    return call
