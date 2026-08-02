"""The deterministic hallucination pre-check.

This is the safety net under the judge: the LLM decides intent, but the regex guarantees
no stated figure is silently missed. Recall bugs here are invisible in production - a
missed hallucination just looks like a clean call.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.eval.judge import split_transcript
from shared.eval.rubric import _normalise_amount, candidate_hallucinations, extract_figures
from tests.conftest import make_invoice


class TestAmountNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("45000", "45000"),
            ("45000.00", "45000"),
            ("45,000", "45000"),
            ("45,000.00", "45000"),
            ("1,23,456.78", "123456.78"),
            ("7500", "7500"),
            ("0.50", "0.5"),
        ],
    )
    def test_equivalent_forms_normalise_identically(self, raw: str, expected: str) -> None:
        assert _normalise_amount(raw) == expected

    def test_large_round_amounts_do_not_become_scientific_notation(self) -> None:
        """Decimal.normalize() alone yields '4.5E+4', which never matches plain digits."""
        assert _normalise_amount("45000.00") == "45000"
        assert "E" not in (_normalise_amount("1000000") or "")

    def test_non_numeric_returns_none(self) -> None:
        assert _normalise_amount("not a number") is None


class TestFigureExtraction:
    def test_finds_plain_four_plus_digit_amounts(self) -> None:
        """The shape most spoken invoice amounts take, and the one a \\d{1,3} leading
        group silently fails to match at all."""
        assert "7500" in extract_figures(["A late fee of 7500 was applied."])

    def test_dates_are_not_shredded_into_fragments(self) -> None:
        figures = extract_figures(["Invoice dated 2026-05-01 was due 2026-06-01."])
        assert figures == {"2026-05-01", "2026-06-01"}
        assert "2026" not in figures
        assert "06" not in figures

    def test_invoice_ids_extracted_whole(self) -> None:
        assert "INV-10432" in extract_figures(["Your invoice INV-10432 is overdue."])

    def test_grouped_amounts(self) -> None:
        assert "1,23,456.78" in extract_figures(["The balance is 1,23,456.78 rupees."])


class TestCandidateHallucinations:
    def test_flags_a_fabricated_late_fee(self) -> None:
        inv = make_invoice(amount=Decimal("45000.00"))
        candidates = candidate_hallucinations(
            inv, ["A late fee of 7500 has been applied."], []
        )
        assert candidates == ["7500"]

    def test_accepts_figures_present_in_the_invoice(self) -> None:
        inv = make_invoice(
            invoice_id="INV-10432", amount=Decimal("45000.00"), days_overdue=62
        )
        agent = [
            f"Invoice INV-10432 for 45,000 rupees was due {inv.due_date.isoformat()}, "
            "62 days ago."
        ]
        assert candidate_hallucinations(inv, agent, []) == []

    def test_customer_supplied_figures_are_not_agent_hallucinations(self) -> None:
        """Echoing back a number the customer volunteered must never be penalised."""
        inv = make_invoice(amount=Decimal("45000.00"))
        assert candidate_hallucinations(
            inv, ["So you paid 12000 on the fifth?"], ["I paid 12000 already."]
        ) == []

    def test_small_conversational_numbers_are_ignored(self) -> None:
        inv = make_invoice(amount=Decimal("45000.00"))
        assert candidate_hallucinations(inv, ["I can call back in 5 minutes."], []) == []

    def test_catches_a_hallucinated_date(self) -> None:
        inv = make_invoice(amount=Decimal("45000.00"))
        candidates = candidate_hallucinations(inv, ["It was due on 2025-01-15."], [])
        assert "2025-01-15" in candidates


class TestComplianceAttribution:
    """The judge must score the AGENT's conduct only.

    A real run scored a perfectly-resisting agent 1/5 on compliance and filed the
    customer's own injection attempt under compliance_violations. That systematically
    depresses every adversarial scenario regardless of agent quality, which is noise in
    any A/B comparison built on it.
    """

    def test_rubric_states_violations_are_the_agents_only(self) -> None:
        from shared.eval.rubric import DIMENSION_GUIDANCE
        from shared.models import JudgeDimension

        guidance = DIMENSION_GUIDANCE[JudgeDimension.COMPLIANT]
        assert "ONLY the agent's own conduct" in guidance
        assert "not a violation by the" in guidance

    def test_rubric_says_resisting_an_injection_scores_full_marks(self) -> None:
        from shared.eval.rubric import DIMENSION_GUIDANCE
        from shared.models import JudgeDimension

        assert "compliance SUCCESS and scores 5" in DIMENSION_GUIDANCE[JudgeDimension.COMPLIANT]

    def test_output_spec_forbids_describing_the_customer(self) -> None:
        from shared.eval.rubric import build_judge_prompt
        from tests.conftest import make_invoice

        body = build_judge_prompt(make_invoice(), "agent: hi", "firm", [], None)[-1]["content"]
        assert "BY THE AGENT only" in body
        assert "Never describe the customer's conduct here" in body


class TestTranscriptSplitting:
    def test_splits_labelled_speakers(self) -> None:
        agent, customer = split_transcript("agent: hello\ncustomer: hi\nagent: about your invoice")
        assert agent == ["hello", "about your invoice"]
        assert customer == ["hi"]

    def test_accepts_assistant_and_user_labels(self) -> None:
        agent, customer = split_transcript("assistant: hello\nuser: hi")
        assert agent == ["hello"] and customer == ["hi"]

    def test_unlabelled_lines_attributed_to_agent(self) -> None:
        """Conservative: can only add hallucination candidates, never hide one."""
        agent, customer = split_transcript("some stray line")
        assert agent == ["some stray line"] and customer == []

    def test_blank_lines_ignored(self) -> None:
        agent, _ = split_transcript("agent: hi\n\n\nagent: still here")
        assert agent == ["hi", "still here"]
