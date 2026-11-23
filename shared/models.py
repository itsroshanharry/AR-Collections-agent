"""Domain models shared by the voice agent and the A/B dashboard.

These are the contract between the two sub-projects: the agent produces CallOutcome
and JudgeScore, the dashboard aggregates them, and both load the same PromptVersion.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Tone(StrEnum):
    GENTLE = "gentle"
    NEUTRAL = "neutral"
    FIRM = "firm"
    ESCALATION = "escalation"


class OutcomeType(StrEnum):
    PROMISE_TO_PAY = "promise_to_pay"
    DISPUTE_RAISED = "dispute_raised"
    ALREADY_PAID = "already_paid"
    PARTIAL_PAYMENT = "partial_payment"
    REFUSED = "refused"
    NO_ANSWER = "no_answer"
    UNCLEAR = "unclear"


class Customer(BaseModel):
    customer_id: str
    name: str
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    gstin: str | None = None
    # IANA name, e.g. "Asia/Kolkata". Optional: the dunning policy falls back to one
    # inferred from the invoice currency when the ledger does not record it.
    timezone: str | None = None


class Invoice(BaseModel):
    """One overdue invoice. This is the ground truth an agent may quote from.

    The judge cross-checks every figure the agent utters against this record, so
    anything not present here counts as a hallucination.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    invoice_id: str
    customer: Customer
    amount: Decimal
    currency: str = "INR"
    invoice_date: date
    due_date: date
    days_overdue: int = Field(ge=0)
    hsn_sac_code: str | None = None
    dispute_flag: bool = False
    reminder_count: int = Field(default=0, ge=0)
    last_reminder_date: date | None = None
    promise_to_pay_date: date | None = None
    risk_tier: RiskTier | None = None

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()

    def context_block(self) -> str:
        """Structured facts injected into the agent prompt.

        The agent is instructed to quote figures ONLY from this block. Keeping it a
        single rendered string means the judge can diff agent utterances against
        exactly what the agent was given.
        """
        lines = [
            f"invoice_number: {self.invoice_id}",
            f"customer_name: {self.customer.name}",
            f"contact_person: {self.customer.contact_name or 'unknown'}",
            f"amount_due: {self.amount} {self.currency}",
            f"invoice_date: {self.invoice_date.isoformat()}",
            f"due_date: {self.due_date.isoformat()}",
            f"days_overdue: {self.days_overdue}",
            f"reminders_already_sent: {self.reminder_count}",
            f"already_disputed: {'yes' if self.dispute_flag else 'no'}",
        ]
        if self.customer.gstin:
            lines.append(f"gstin: {self.customer.gstin}")
        if self.hsn_sac_code:
            lines.append(f"hsn_sac_code: {self.hsn_sac_code}")
        return "\n".join(lines)

    def quotable_figures(self) -> set[str]:
        """Every number/date the agent is allowed to state, normalised for matching."""
        figures = {
            self.invoice_id,
            str(self.amount),
            f"{self.amount:.2f}",
            f"{self.amount:,.2f}",
            str(int(self.amount)),
            self.invoice_date.isoformat(),
            self.due_date.isoformat(),
            str(self.days_overdue),
            str(self.reminder_count),
        }
        if self.customer.gstin:
            figures.add(self.customer.gstin)
        if self.hsn_sac_code:
            figures.add(self.hsn_sac_code)
        return {f for f in figures if f}


class PromptVersion(BaseModel):
    """A versioned prompt template. Loaded identically by the agent and the dashboard."""

    id: str
    name: str
    version: int = Field(ge=1)
    tone: Tone
    applies_to_tier: list[RiskTier] = Field(default_factory=list)
    template: str
    changelog: str = ""
    created_at: datetime | None = None

    @property
    def label(self) -> str:
        return f"{self.name}@v{self.version}"

    def render(self, invoice: Invoice, **extra: str) -> str:
        return self.template.format(
            invoice_context=invoice.context_block(),
            customer_name=invoice.customer.name,
            contact_name=invoice.customer.contact_name or "there",
            amount_due=f"{invoice.amount} {invoice.currency}",
            invoice_number=invoice.invoice_id,
            days_overdue=invoice.days_overdue,
            due_date=invoice.due_date.isoformat(),
            **extra,
        )


class CallOutcome(BaseModel):
    """Structured result extracted from a call transcript."""

    outcome: OutcomeType
    promise_to_pay_date: date | None = None
    promised_amount: Decimal | None = None
    disputed_amount: Decimal | None = None
    dispute_reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""


class JudgeDimension(StrEnum):
    ON_TOPIC = "on_topic"
    TONE_APPROPRIATE = "tone_appropriate"
    EXTRACTION_CORRECT = "extraction_correct"
    NO_HALLUCINATION = "no_hallucination"
    COMPLIANT = "compliant"


class JudgeScore(BaseModel):
    """LLM-as-judge verdict on one transcript.

    Scores are 1-5 per dimension. `hallucinated_figures` holds any amount/date/invoice
    number the agent stated that is absent from the Invoice record.
    """

    scores: dict[JudgeDimension, int]
    hallucinated_figures: list[str] = Field(default_factory=list)
    compliance_violations: list[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("scores")
    @classmethod
    def _validate_scores(cls, v: dict[JudgeDimension, int]) -> dict[JudgeDimension, int]:
        for dim, score in v.items():
            if not 1 <= score <= 5:
                raise ValueError(f"score for {dim} must be 1-5, got {score}")
        return v

    @property
    def composite(self) -> float:
        """Mean across dimensions. This is what win-rate compares."""
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)

    @property
    def hallucinated(self) -> bool:
        return bool(self.hallucinated_figures)


class PairwiseVerdict(BaseModel):
    """Head-to-head preference between two transcripts for the same scenario.

    The judge sees them as "first"/"second" with presentation order randomised and all
    version identifiers stripped, so `winner` is in *presentation* space. The runner
    maps it back to a variant. Absolute per-dimension scores are collected separately;
    this exists because independent absolute scoring is poorly discriminative when two
    prompts are both competent.
    """

    winner: Literal["first", "second", "tie"]
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class EvalScenario(BaseModel):
    """One fixed benchmark case: an invoice plus a scripted customer and ground truth."""

    scenario_id: str
    description: str
    invoice: Invoice
    customer_persona: str
    customer_script: list[str] = Field(default_factory=list)
    expected_outcome: OutcomeType
    expected_promise_date: date | None = None
    is_adversarial: bool = False
    tags: list[str] = Field(default_factory=list)
