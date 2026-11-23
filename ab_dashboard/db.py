"""Persistence for prompt versions, experiments, results and call logs.

Runs on SQLite locally and Postgres (Neon) when deployed, from one model set. That
constrains column types to the portable intersection - JSON not JSONB, no ARRAY, no
server-side UUID defaults. The test suite runs against both dialects so a
Postgres-only construct fails at commit time rather than at deploy time.

Render's free Postgres is deleted 30 days after creation; use Neon for anything you
intend to demo more than a month from now.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from shared.config import get_settings

logger = logging.getLogger(__name__)


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

    # The run's inputs, persisted so any worker can resume it after a restart without
    # the original request body - and so a result can always be reproduced exactly.
    seed: Mapped[int] = mapped_column(Integer, default=42, server_default="42")
    scenario_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Lease: which worker currently owns this run, and when it last proved it was alive.
    # A run whose heartbeat goes stale is reclaimed by another worker (see jobs.py).
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list[ResultRow]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", lazy="selectin"
    )


class ResultRow(Base):
    """One scenario x one variant. Every raw response is kept so a headline
    win-rate can always be traced back to the transcript that produced it."""

    __tablename__ = "experiment_results"
    # Results are written one scenario at a time as they finish, and a resumed run skips
    # scenarios already stored. This makes a double-write impossible rather than unlikely.
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "scenario_id", "variant_label", name="uq_result_scenario_variant"
        ),
    )

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


# --------------------------------------------------------------------------------------
# Dunning workflow
# --------------------------------------------------------------------------------------


class InvoiceStateRow(Base):
    """Where one invoice stands in the dunning workflow. The state machine's Snapshot,
    persisted. See workflow/state_machine.py for the states and what moves between them."""

    __tablename__ = "invoice_states"

    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    # The invoice as synced from the ledger. Calls are rendered from it and the judge
    # grounds against it, so it is the record of what the agent was allowed to say.
    invoice: Mapped[dict] = mapped_column(JSON)
    # Money as text: SQLite has no decimal type and a float would round it.
    amount_outstanding: Mapped[str] = mapped_column(String(32))
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)

    state: Mapped[str] = mapped_column(String(24), index=True)
    tier: Mapped[str] = mapped_column(String(16))
    cadence_tier: Mapped[str] = mapped_column(String(16))
    cadence_step: Mapped[int] = mapped_column(Integer, default=0)
    cadence_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promise_to_pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    broken_promises: Mapped[int] = mapped_column(Integer, default=0)
    escalations: Mapped[int] = mapped_column(Integer, default=0)
    closed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    next_action_channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Ageing: days overdue at sync time, and the date of that sync. Days overdue at any
    # later date is derived from these rather than frozen at the dataset's snapshot.
    snapshot_days_overdue: Mapped[int] = mapped_column(Integer)
    synced_on: Mapped[date] = mapped_column(Date)

    # Optimistic locking: the scheduler, the voice agent and a person resolving a task
    # can all touch one invoice at once. A write based on a stale read fails instead of
    # silently overwriting the other one's change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": version}


class ContactAttemptRow(Base):
    """Every time the customer was contacted. The frequency caps are computed from this."""

    __tablename__ = "contact_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoice_states.invoice_id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    call_log_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TaskRow(Base):
    """Follow-up work for a person, opened by the workflow (see state_machine.TaskSpec)."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoice_states.invoice_id", ondelete="CASCADE"), index=True
    )
    task_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    assignee: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # Idempotency: the same call re-processed, or the same broken promise detected by two
    # sweeps, maps to the same key and so cannot open a second task.
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    call_log_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvoiceEventRow(Base):
    """Append-only audit trail: every transition, with the state before and after."""

    __tablename__ = "invoice_events"

    # Integer, not a uuid: several events share one timestamp (sync, then disputed on
    # arrival), and the autoincrement id is what keeps the timeline in order.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoice_states.invoice_id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    tier: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
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


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# The revision that matches a database built by the old `create_all` path. Must only
# ever be the first revision: stamping a later one would skip real schema changes.
BASELINE_REVISION = "0001"


def _alembic_config(connection: Connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def _upgrade(connection: Connection) -> None:
    cfg = _alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in tables and "experiments" in tables:
        # Created by create_all before migrations existed. Its tables already match the
        # baseline, so record that rather than trying to create them a second time.
        logger.warning("Unversioned database found; stamping baseline %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """Bring the schema to the latest migration.

    Runs on every startup and is a no-op when already current. `create_all` is not used
    here: it creates missing tables but never alters existing ones, so a new column
    would silently be absent on any database that predates it.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
