"""Readiness: can this instance actually do its job right now?

Liveness (`/health`) only says the process is up. Readiness (`/health/ready`) checks the
things that make it useful, so a load balancer stops routing to an instance that cannot
reach its database, and an alert fires when the scheduler silently stops:

  * database   - a round trip succeeds
  * schema     - migrations are at head (an instance on an old schema writes bad rows)
  * scheduler  - the last dunning pass finished recently, if the scheduler is enabled

The LLM provider is reported but never fails readiness: the workflow, tasks and metrics
all work without one, and only new experiments need it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from ab_dashboard.db import MIGRATIONS_DIR, get_engine

# A pass counts as stale after this many missed intervals.
STALE_AFTER_INTERVALS = 3


@dataclass
class SchedulerState:
    """Updated by the scheduler loop; read by the readiness check."""

    started_at: float = field(default_factory=time.time)
    last_success: float | None = None
    last_error: str | None = None


scheduler_state = SchedulerState()


@dataclass
class Check:
    ok: bool
    detail: str
    latency_ms: float | None = None


async def check_database() -> Check:
    started = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        return Check(False, f"{type(exc).__name__}: {exc}")
    return Check(True, "reachable", round((time.perf_counter() - started) * 1000, 1))


async def check_schema() -> Check:
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    try:
        async with get_engine().connect() as conn:
            current = await conn.run_sync(
                lambda c: MigrationContext.configure(c).get_current_revision()
            )
    except Exception as exc:  # noqa: BLE001
        return Check(False, f"{type(exc).__name__}: {exc}")
    if current != head:
        return Check(False, f"schema at {current}, code expects {head}")
    return Check(True, f"at head {head}")


def check_scheduler(interval_seconds: float, *, now: float | None = None) -> Check:
    if interval_seconds <= 0:
        return Check(True, "disabled (DUNNING_TICK_SECONDS=0)")
    now = now or time.time()
    limit = interval_seconds * STALE_AFTER_INTERVALS
    state = scheduler_state
    if state.last_success is None:
        if now - state.started_at < limit:
            return Check(True, "starting; no pass completed yet")
        return Check(False, f"no pass has completed since start ({state.last_error or 'no error'})")
    age = now - state.last_success
    if age > limit:
        return Check(False, f"last pass {age:.0f}s ago, limit {limit:.0f}s ({state.last_error})")
    return Check(True, f"last pass {age:.0f}s ago")
