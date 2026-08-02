"""Persistence for prompt versions, experiments, results and call logs.

Runs on SQLite locally and Postgres (Neon) when deployed, from one model set. That
constrains column types to the portable intersection - JSON not JSONB, no ARRAY, no
server-side UUID defaults. The test suite runs against both dialects so a
Postgres-only construct fails at commit time rather than at deploy time.

Render's free Postgres is deleted 30 days after creation; use Neon for anything you
intend to demo more than a month from now.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from shared.config import get_settings


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class ExperimentStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PromptVersionRow(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    tone: Mapped[str] = mapped_column(String(32))
    template: Mapped[str] = mapped_column(Text)
    changelog: Mapped[str] = mapped_column(Text, default="")
    applies_to_tier: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ExperimentRow(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    variant_a_id: Mapped[str] = mapped_column(String(128))
    variant_b_id: Mapped[str] = mapped_column(String(128))
    eval_set: Mapped[str] = mapped_column(String(255), default="eval_scenarios.yaml")
    status: Mapped[str] = mapped_column(String(16), default=ExperimentStatus.PENDING, index=True)
    scenario_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list[ResultRow]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", lazy="selectin"
    )


class ResultRow(Base):
    """One scenario x one variant. Every raw response is kept so a headline
    win-rate can always be traced back to the transcript that produced it."""

    __tablename__ = "experiment_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(String(128), index=True)
    variant_label: Mapped[str] = mapped_column(String(1))  # "A" or "B"
    prompt_id: Mapped[str] = mapped_column(String(128), index=True)

    transcript: Mapped[str] = mapped_column(Text, default="")
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    composite: Mapped[float] = mapped_column(Float, default=0.0)
    hallucinated: Mapped[bool] = mapped_column(Boolean, default=False)
    hallucinated_figures: Mapped[list] = mapped_column(JSON, default=list)
    compliance_violations: Mapped[list] = mapped_column(JSON, default=list)
    judge_rationale: Mapped[str] = mapped_column(Text, default="")

    extracted_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome_match: Mapped[bool] = mapped_column(Boolean, default=False)
    promise_date_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    total_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    mean_turn_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(32), default="")
    is_adversarial: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # The position-blind head-to-head verdict, denormalised onto both rows of the pair.
    # Without persisting this, /results could only fall back to comparing absolute
    # composite scores - which is exactly the weak, poorly-discriminative signal the
    # pairwise judge exists to replace.
    pairwise_winner: Mapped[str | None] = mapped_column(String(4), nullable=True)  # A|B|tie
    pairwise_reason: Mapped[str] = mapped_column(Text, default="")
    presented_a_first: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    experiment: Mapped[ExperimentRow] = relationship(back_populates="results")


class CallLogRow(Base):
    """A real (voice or simulated) collections call, as opposed to an experiment run."""

    __tablename__ = "call_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    prompt_id: Mapped[str] = mapped_column(String(128), index=True)
    risk_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="simulate")  # simulate | voice

    transcript: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    promise_to_pay_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    outcome_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    composite: Mapped[float] = mapped_column(Float, default=0.0)
    hallucinated: Mapped[bool] = mapped_column(Boolean, default=False)
    hallucinated_figures: Mapped[list] = mapped_column(JSON, default=list)
    compliance_violations: Mapped[list] = mapped_column(JSON, default=list)

    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


def normalise_database_url(url: str) -> str:
    """Coerce a provider-supplied URL into an async SQLAlchemy DSN.

    Neon and Render both hand out `postgres://...`, which SQLAlchemy rejects outright,
    and `postgresql://...`, which silently selects the *sync* driver and then deadlocks
    the event loop on first query.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif url.startswith("sqlite://") and "+aiosqlite" not in url:
        url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        url = normalise_database_url(get_settings().database_url)
        _engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
