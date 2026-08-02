"""LLM-as-judge scoring for collections call transcripts."""

from __future__ import annotations

import logging

from shared.eval.rubric import (
    build_judge_prompt,
    build_pairwise_prompt,
    candidate_hallucinations,
)
from shared.llm_client import LLMClient, LLMResponse
from shared.models import Invoice, JudgeDimension, JudgeScore, OutcomeType, PairwiseVerdict

logger = logging.getLogger(__name__)

AGENT_SPEAKER = "agent"
CUSTOMER_SPEAKER = "customer"


def split_transcript(transcript: str) -> tuple[list[str], list[str]]:
    """Split a `speaker: text` transcript into agent and customer turns.

    Unlabelled lines are attributed to the agent - the conservative choice, since it
    can only ever increase the agent's hallucination candidates, never hide one.
    """
    agent_turns: list[str] = []
    customer_turns: list[str] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        speaker, _, text = line.partition(":")
        speaker_key = speaker.strip().lower()
        if speaker_key.startswith(CUSTOMER_SPEAKER) or speaker_key.startswith("user"):
            customer_turns.append(text.strip())
        elif speaker_key.startswith(AGENT_SPEAKER) or speaker_key.startswith("assistant"):
            agent_turns.append(text.strip())
        else:
            agent_turns.append(line)
    return agent_turns, customer_turns


async def judge_transcript(
    client: LLMClient,
    invoice: Invoice,
    transcript: str,
    *,
    assigned_tone: str,
    expected_outcome: OutcomeType | None = None,
) -> tuple[JudgeScore, LLMResponse]:
    agent_turns, customer_turns = split_transcript(transcript)
    candidates = candidate_hallucinations(invoice, agent_turns, customer_turns)

    if candidates:
        logger.debug("Grounding pre-check flagged %d candidate figure(s)", len(candidates))

    messages = build_judge_prompt(
        invoice=invoice,
        transcript=transcript,
        assigned_tone=assigned_tone,
        candidates=candidates,
        expected_outcome=expected_outcome.value if expected_outcome else None,
    )

    score, resp = await client.complete_structured(
        messages,
        JudgeScore,
        # Routed to the small model: rate limits are per-model, so scoring gets its own
        # token budget rather than competing with dialogue generation. Rubric scoring is
        # a structured extraction task, which the 20B handles reliably.
        use_small=True,
        # Enough headroom that reasoning cannot starve the JSON into a
        # json_validate_failed 400, but no more: max_tokens is *reserved* against the
        # per-minute quota, so an over-generous budget throttles the whole run. 2500 here
        # meant a single judge call reserved most of the 8K/min window.
        reasoning_effort="low",
        temperature=0.0,
        max_tokens=1200,
    )

    missing = set(JudgeDimension) - set(score.scores)
    if missing:
        raise ValueError(f"Judge omitted dimension(s): {sorted(d.value for d in missing)}")

    # The pre-check is deterministic and cannot miss a figure; the judge can only ever
    # narrow that list, never widen it beyond what was actually said.
    invented = [f for f in score.hallucinated_figures if f not in candidates]
    if invented:
        logger.warning("Judge reported figures absent from the pre-check: %s", invented)
        score.hallucinated_figures = [
            f for f in score.hallucinated_figures if f in candidates
        ]

    return score, resp


async def judge_pairwise(
    client: LLMClient,
    invoice: Invoice,
    transcript_a: str,
    transcript_b: str,
    *,
    assigned_tone: str,
    expected_outcome: OutcomeType | None = None,
    present_a_first: bool,
) -> tuple[str, PairwiseVerdict, LLMResponse]:
    """Compare variant A against variant B, returning the winner as "A", "B" or "tie".

    `present_a_first` is supplied by the caller (seeded per scenario) rather than
    randomised here, so a run is reproducible and the order actually used is auditable.
    The judge only ever sees "first"/"second"; the mapping back to A/B happens here.
    """
    first, second = (
        (transcript_a, transcript_b) if present_a_first else (transcript_b, transcript_a)
    )

    messages = build_pairwise_prompt(
        invoice=invoice,
        transcript_first=first,
        transcript_second=second,
        assigned_tone=assigned_tone,
        expected_outcome=expected_outcome.value if expected_outcome else None,
    )

    # Kept on the large model: the head-to-head preference is the single most
    # discriminative signal in the whole experiment, and it compares two full
    # transcripts. This is not the place to economise on capability.
    verdict, resp = await client.complete_structured(
        messages,
        PairwiseVerdict,
        reasoning_effort="medium",
        temperature=0.0,
        max_tokens=900,
    )

    if verdict.winner == "tie":
        winner = "tie"
    elif verdict.winner == "first":
        winner = "A" if present_a_first else "B"
    else:
        winner = "B" if present_a_first else "A"

    return winner, verdict, resp
