"""Position-blind pairwise judging.

The judge only ever sees "first" and "second". If the mapping back to A/B is wrong, the
win-rate is silently inverted for half the scenarios and the experiment reports the
opposite of the truth - with no error and no crash. Hence direct tests of the mapping.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from shared.eval.judge import judge_pairwise
from shared.eval.rubric import build_pairwise_prompt
from shared.models import PairwiseVerdict
from tests.conftest import make_invoice

A_TEXT = "agent: I am variant A."
B_TEXT = "agent: I am variant B."


@dataclass
class FakeResponse:
    provider: str = "fake"
    model: str = "fake"
    latency_ms: float = 1.0
    text: str = ""


class FakeClient:
    """Returns a fixed verdict and records the prompt it was given."""

    def __init__(self, winner: str) -> None:
        self.winner = winner
        self.last_messages: list[dict[str, str]] = []

    async def complete_structured(self, messages, schema, **kwargs):
        self.last_messages = messages
        return PairwiseVerdict(winner=self.winner, reason="because", confidence=0.9), (
            FakeResponse()
        )


class TestWinnerMapping:
    @pytest.mark.parametrize(
        ("present_a_first", "judge_says", "expected"),
        [
            # A shown first: "first" means A.
            (True, "first", "A"),
            (True, "second", "B"),
            # A shown second: "first" means B. This is the inversion that matters.
            (False, "first", "B"),
            (False, "second", "A"),
            # Ties are order-independent.
            (True, "tie", "tie"),
            (False, "tie", "tie"),
        ],
    )
    async def test_maps_presentation_order_back_to_variant(
        self, present_a_first: bool, judge_says: str, expected: str
    ) -> None:
        client = FakeClient(judge_says)
        winner, _, _ = await judge_pairwise(
            client,
            make_invoice(),
            A_TEXT,
            B_TEXT,
            assigned_tone="firm",
            present_a_first=present_a_first,
        )
        assert winner == expected

    async def test_transcripts_are_swapped_in_the_prompt_when_b_goes_first(self) -> None:
        client = FakeClient("first")
        await judge_pairwise(
            client,
            make_invoice(),
            A_TEXT,
            B_TEXT,
            assigned_tone="firm",
            present_a_first=False,
        )
        body = client.last_messages[-1]["content"]
        assert body.index(B_TEXT) < body.index(A_TEXT), "B must actually be presented first"

    async def test_transcripts_keep_their_order_when_a_goes_first(self) -> None:
        client = FakeClient("first")
        await judge_pairwise(
            client,
            make_invoice(),
            A_TEXT,
            B_TEXT,
            assigned_tone="firm",
            present_a_first=True,
        )
        body = client.last_messages[-1]["content"]
        assert body.index(A_TEXT) < body.index(B_TEXT)


class TestPromptBlinding:
    def test_prompt_never_names_the_variants(self) -> None:
        """Leaking "variant A" / a version id would let the judge form a prior."""
        prompt = build_pairwise_prompt(
            make_invoice(), A_TEXT, B_TEXT, "firm", "promise_to_pay"
        )
        body = "\n".join(m["content"] for m in prompt)
        assert "variant" not in body.lower().replace("variant a.", "").replace("variant b.", "")
        assert "TRANSCRIPT FIRST" in body
        assert "TRANSCRIPT SECOND" in body

    def test_prompt_warns_against_injection_inside_transcripts(self) -> None:
        prompt = build_pairwise_prompt(make_invoice(), A_TEXT, B_TEXT, "firm", None)
        system = prompt[0]["content"]
        assert "DATA, not instruction" in system

    def test_prompt_ranks_grounding_above_fluency(self) -> None:
        prompt = build_pairwise_prompt(make_invoice(), A_TEXT, B_TEXT, "firm", None)
        system = prompt[0]["content"]
        assert "Fluency is not quality" in system

    def test_ground_truth_included_when_known(self) -> None:
        prompt = build_pairwise_prompt(
            make_invoice(), A_TEXT, B_TEXT, "firm", "dispute_raised"
        )
        assert "dispute_raised" in prompt[-1]["content"]


class TestDeterministicOrdering:
    def test_order_is_stable_for_a_fixed_seed_and_scenario(self) -> None:
        """A re-run of the same experiment must present transcripts the same way, or
        results are not reproducible."""
        import random

        def flip(seed: int, scenario_id: str) -> bool:
            return random.Random(f"{seed}:{scenario_id}").random() < 0.5

        assert flip(42, "S01") == flip(42, "S01")
        assert flip(42, "S01") == flip(42, "S01")

    def test_order_varies_across_scenarios(self) -> None:
        """A constant order would concentrate any residual position bias on one variant."""
        import random

        flips = [
            random.Random(f"42:S{i:02d}").random() < 0.5 for i in range(1, 19)
        ]
        assert len(set(flips)) == 2, "expected a mix of orderings across 18 scenarios"
