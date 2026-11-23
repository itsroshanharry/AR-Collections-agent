"""Durable experiment execution: leases, heartbeats, per-scenario persistence, resume.

An experiment takes minutes (the free tier paces it to roughly one scenario a minute),
and the host it runs on is not stable: Render's free instances spin down when idle, and
any deploy restarts the process. FastAPI's BackgroundTasks lives in memory, so every
restart used to kill the run silently and leave it `running` forever, with nothing saved
because results were only written once the whole run finished.

This module makes a run survive that:

  * **Per-scenario persistence.** Each scenario's two result rows are committed the
    moment its comparison finishes. A crash loses at most the scenario in flight.
  * **Resume.** A restarted run skips every scenario already stored. The unique
    constraint on (experiment, scenario, variant) makes a double write impossible.
  * **Leases.** The database is the queue. A worker claims a run with one conditional
    UPDATE, which is atomic on both SQLite and Postgres, so this needs no Redis and no
    `SELECT ... FOR UPDATE SKIP LOCKED` (which SQLite lacks). The owner renews a
    heartbeat; if it dies without releasing, the lease goes stale and any worker
    reclaims the run.
  * **Reaper.** A periodic sweep claims pending runs and runs whose lease has expired, so
    orphaned work is picked up without anyone re-submitting it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update

from ab_dashboard.db import (
    ExperimentRow,
    ExperimentStatus,
    PromptVersionRow,
    ResultRow,
    get_sessionmaker,
)
from ab_dashboard.experiment_runner import (
    ScenarioComparison,
    VariantRun,
    run_experiment,
)
from shared.config import get_settings
from shared.eval.eval_set import load_eval_set
from shared.eval.metrics import promise_date_correct
from shared.llm_client import LLMClient
from shared.models import PromptVersion, Tone

logger = logging.getLogger(__name__)

# Identifies this process as a lease holder. Host + pid makes the owner of a stuck run
# readable in the database; the random suffix keeps it unique across restarts.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"[:64]

HEARTBEAT_SECONDS = 15.0
# Long enough that a slow commit on a busy SQLite file cannot expire a live lease;
# short enough that a crashed run is picked up within a couple of minutes.
LEASE_SECONDS = 90.0
REAPER_INTERVAL_SECONDS = 30.0

# Runs owned by this process. Prevents the reaper and the create endpoint from both
# starting the same run here, since a lease check alone cannot tell them apart.
_active: dict[str, asyncio.Task[None]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def row_to_prompt(row: PromptVersionRow) -> PromptVersion:
    return PromptVersion(
        id=row.id,
        name=row.name,
        version=row.version,
        tone=Tone(row.tone),
        template=row.template,
        changelog=row.changelog,
        applies_to_tier=list(row.applies_to_tier or []),
    )


def result_row(experiment_id: str, comparison: ScenarioComparison, run: VariantRun) -> ResultRow:
    score = run.score
    return ResultRow(
        experiment_id=experiment_id,
        scenario_id=comparison.scenario_id,
        variant_label=run.variant_label,
        prompt_id=run.prompt_id,
        transcript=run.transcript,
        scores={k.value: v for k, v in (score.scores if score else {}).items()},
        composite=run.composite,
        hallucinated=run.hallucinated,
        hallucinated_figures=list(score.hallucinated_figures) if score else [],
        compliance_violations=list(score.compliance_violations) if score else [],
        judge_rationale=score.rationale if score else "",
        extracted_outcome=run.extracted_outcome.value if run.extracted_outcome else None,
        expected_outcome=comparison.expected_outcome.value,
        outcome_match=run.extracted_outcome == comparison.expected_outcome,
        # Exact match against the scenario's ground-truth date; None when it declares none.
        promise_date_match=promise_date_correct(
            run.promise_to_pay_date, comparison.expected_promise_date
        ),
        total_latency_ms=run.total_latency_ms,
        mean_turn_latency_ms=run.mean_turn_latency_ms,
        provider=run.provider,
        is_adversarial=comparison.is_adversarial,
        error=run.error,
        pairwise_winner=comparison.pairwise_winner,
        pairwise_reason=comparison.pairwise_reason,
        presented_a_first=comparison.presented_a_first,
    )


# --------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------


def _claimable():
    """Runs nobody live owns: unowned, already ours, or with an expired heartbeat."""
    stale_before = _now() - timedelta(seconds=LEASE_SECONDS)
    return (
        ExperimentRow.status.in_([ExperimentStatus.PENDING, ExperimentStatus.RUNNING]),
        or_(
            ExperimentRow.worker_id.is_(None),
            ExperimentRow.worker_id == WORKER_ID,
            ExperimentRow.heartbeat_at.is_(None),
            ExperimentRow.heartbeat_at < stale_before,
        ),
    )


async def claim(experiment_id: str) -> bool:
    """Take the lease on one run. Atomic: of two workers racing, exactly one wins."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id, *_claimable())
            .values(
                status=ExperimentStatus.RUNNING,
                worker_id=WORKER_ID,
                heartbeat_at=_now(),
            )
        )
        await session.commit()
        return result.rowcount == 1


async def _renew(experiment_id: str) -> bool:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            update(ExperimentRow)
            .where(
                ExperimentRow.id == experiment_id,
                ExperimentRow.worker_id == WORKER_ID,
                ExperimentRow.status == ExperimentStatus.RUNNING,
            )
            .values(heartbeat_at=_now())
        )
        await session.commit()
        return result.rowcount == 1


async def _heartbeat(experiment_id: str, run_task: asyncio.Task) -> None:
    """Renew the lease until cancelled. If it was lost, stop the run: another worker now
    owns it, and two writers on one run would race on its results."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            still_ours = await _renew(experiment_id)
        except Exception:  # noqa: BLE001 - a transient DB error is not a lost lease
            logger.warning("Heartbeat for %s failed; retrying", experiment_id, exc_info=True)
            continue
        if not still_ours:
            logger.error("Lost the lease on %s; stopping this copy of the run", experiment_id)
            run_task.cancel()
            return


async def release_leases() -> int:
    """Hand back every run this process holds, so a restart resumes them immediately
    instead of waiting out the lease. Status stays `running`: the work is unfinished."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            update(ExperimentRow)
            .where(
                ExperimentRow.worker_id == WORKER_ID,
                ExperimentRow.status == ExperimentStatus.RUNNING,
            )
            .values(worker_id=None, heartbeat_at=None)
        )
        await session.commit()
        return result.rowcount


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------


async def _finish(experiment_id: str, *, error: str | None = None) -> None:
    """Mark the run terminal - only if we still hold it, so a worker that lost its lease
    cannot overwrite the new owner's outcome."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id, ExperimentRow.worker_id == WORKER_ID)
            .values(
                status=ExperimentStatus.FAILED if error else ExperimentStatus.COMPLETED,
                error=error,
                completed_at=_now(),
                worker_id=None,
                heartbeat_at=None,
            )
        )
        await session.commit()


async def _persist_comparison(experiment_id: str, comparison: ScenarioComparison) -> None:
    async with get_sessionmaker()() as session:
        already = await session.scalar(
            select(func.count(ResultRow.id)).where(
                ResultRow.experiment_id == experiment_id,
                ResultRow.scenario_id == comparison.scenario_id,
            )
        )
        if already:
            logger.warning("%s/%s already stored; skipping", experiment_id, comparison.scenario_id)
            return
        session.add(result_row(experiment_id, comparison, comparison.a))
        session.add(result_row(experiment_id, comparison, comparison.b))
        # Incremented in SQL, not read-modify-write: scenarios finish concurrently.
        await session.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id)
            .values(completed_count=ExperimentRow.completed_count + 1, heartbeat_at=_now())
        )
        await session.commit()


async def execute(experiment_id: str) -> None:
    """Run (or resume) one experiment. Assumes the caller holds the lease."""
    settings = get_settings()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        experiment = await session.get(ExperimentRow, experiment_id)
        if experiment is None:
            return
        row_a = await session.get(PromptVersionRow, experiment.variant_a_id)
        row_b = await session.get(PromptVersionRow, experiment.variant_b_id)
        if row_a is None or row_b is None:
            await _finish(experiment_id, error="One or both prompt versions no longer exist.")
            return
        prompt_a, prompt_b = row_to_prompt(row_a), row_to_prompt(row_b)
        seed, limit = experiment.seed, experiment.scenario_limit

        done = set(
            (
                await session.execute(
                    select(ResultRow.scenario_id)
                    .where(ResultRow.experiment_id == experiment_id)
                    .distinct()
                )
            ).scalars()
        )

    scenarios = load_eval_set()
    if limit:
        scenarios = scenarios[:limit]
    remaining = [s for s in scenarios if s.scenario_id not in done]

    async with sessionmaker() as session:
        experiment = await session.get(ExperimentRow, experiment_id)
        if experiment is None:
            return
        experiment.scenario_count = len(scenarios)
        # Recounted from stored rows rather than trusted: the old counter may be from a
        # write that never committed.
        experiment.completed_count = len(done & {s.scenario_id for s in scenarios})
        await session.commit()

    if done:
        logger.info(
            "Resuming %s: %d of %d scenario(s) already stored",
            experiment_id,
            len(scenarios) - len(remaining),
            len(scenarios),
        )

    if remaining:
        client = LLMClient(settings)
        try:
            await run_experiment(
                client,
                prompt_a,
                prompt_b,
                remaining,
                max_concurrency=settings.max_concurrent_llm_calls,
                seed=seed,
                progress_cb=lambda c: _persist_comparison(experiment_id, c),
            )
        finally:
            await client.aclose()

    await _finish(experiment_id)
    logger.info("Experiment %s completed", experiment_id)


async def _supervise(experiment_id: str) -> None:
    run_task = asyncio.current_task()
    assert run_task is not None
    heartbeat = asyncio.create_task(_heartbeat(experiment_id, run_task))
    try:
        await execute(experiment_id)
    except asyncio.CancelledError:
        # Shutdown or a lost lease. Leave it `running` with its results so far; the next
        # owner resumes from there.
        logger.info("Experiment %s interrupted; it will resume", experiment_id)
        raise
    except Exception as exc:  # noqa: BLE001 - must record the failure, not vanish
        logger.exception("Experiment %s failed", experiment_id)
        await _finish(experiment_id, error=f"{type(exc).__name__}: {exc}")
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        _active.pop(experiment_id, None)


async def launch(experiment_id: str) -> bool:
    """Claim and start a run in this process. False if it is already running anywhere."""
    if experiment_id in _active:
        return False
    if not await claim(experiment_id):
        return False
    # Re-check: the reaper may have claimed the same id while we awaited.
    if experiment_id in _active:
        return False
    _active[experiment_id] = asyncio.create_task(
        _supervise(experiment_id), name=f"experiment-{experiment_id}"
    )
    return True


async def reap_once() -> list[str]:
    """Start every run that has no live owner. Returns the ids this worker took."""
    async with get_sessionmaker()() as session:
        candidates = list(
            (
                await session.execute(
                    select(ExperimentRow.id).where(*_claimable()).order_by(ExperimentRow.created_at)
                )
            ).scalars()
        )
    started = []
    for experiment_id in candidates:
        if await launch(experiment_id):
            started.append(experiment_id)
    if started:
        logger.info("Picked up %d orphaned or pending run(s): %s", len(started), started)
    return started


async def reaper_loop() -> None:
    while True:
        try:
            await reap_once()
        except Exception:  # noqa: BLE001 - a failed sweep must not stop future sweeps
            logger.warning("Reaper sweep failed", exc_info=True)
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)


async def shutdown() -> None:
    """Stop this process's runs and hand their leases back."""
    tasks = list(_active.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    released = await release_leases()
    if released:
        logger.info("Released %d run lease(s) for resumption", released)
