"""Schema migrations and durable experiment execution.

Everything here runs against a real (throwaway) SQLite file, because the properties
under test - atomic lease claims, unique constraints, schema parity - live in the
database, not in Python.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, inspect, select, text, update

from ab_dashboard import jobs
from ab_dashboard.db import (
    MIGRATIONS_DIR,
    Base,
    ExperimentRow,
    ExperimentStatus,
    PromptVersionRow,
    ResultRow,
    _alembic_config,
)
from ab_dashboard.experiment_runner import ScenarioComparison, VariantRun
from shared.eval.eval_set import load_eval_set
from tests.conftest import perfect_score


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Point the app's engine at a fresh SQLite file; yield its sessionmaker."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    yield db_module
    await db_module.dispose_engine()
    get_settings.cache_clear()


def _schema_diff(connection) -> list:
    return compare_metadata(MigrationContext.configure(connection), Base.metadata)


class TestMigrations:
    async def test_migrations_produce_exactly_the_models_schema(self, db) -> None:
        # If someone edits a model without writing a migration, this fails - instead of
        # the column being silently absent on every deployed database.
        await db.init_db()
        async with db.get_engine().connect() as conn:
            diff = await conn.run_sync(_schema_diff)
        assert diff == []

    async def test_init_db_is_idempotent(self, db) -> None:
        await db.init_db()
        await db.init_db()

    async def test_legacy_create_all_database_is_baselined_then_upgraded(self, db) -> None:
        # A database built before migrations existed: baseline tables, no version table.
        async with db.get_engine().begin() as conn:
            await conn.run_sync(lambda c: command.upgrade(_alembic_config(c), "0001"))
            await conn.execute(text("DROP TABLE alembic_version"))

        await db.init_db()

        async with db.get_engine().connect() as conn:
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("experiments")}
            )
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert {"seed", "scenario_limit", "worker_id", "heartbeat_at"} <= columns
        assert version == ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


# --------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------


async def _seed_experiment(db, *, limit: int = 3) -> str:
    async with db.get_sessionmaker()() as session:
        for pid in ("p_a", "p_b"):
            session.add(
                PromptVersionRow(
                    id=pid, name=pid, version=1, tone="firm", template="x", applies_to_tier=[]
                )
            )
        experiment = ExperimentRow(
            variant_a_id="p_a",
            variant_b_id="p_b",
            scenario_limit=limit,
            status=ExperimentStatus.PENDING,
        )
        session.add(experiment)
        await session.commit()
        return experiment.id


def _comparison(scenario) -> ScenarioComparison:
    def run(label: str) -> VariantRun:
        return VariantRun(
            scenario_id=scenario.scenario_id,
            variant_label=label,
            prompt_id=f"p_{label.lower()}",
            transcript="agent: hello",
            score=perfect_score(),
            extracted_outcome=scenario.expected_outcome,
            promise_to_pay_date=scenario.expected_promise_date,
        )

    return ScenarioComparison(
        scenario_id=scenario.scenario_id,
        is_adversarial=scenario.is_adversarial,
        expected_outcome=scenario.expected_outcome,
        expected_promise_date=scenario.expected_promise_date,
        a=run("A"),
        b=run("B"),
    )


class _NullClient:
    def __init__(self, *_a, **_k) -> None: ...

    async def aclose(self) -> None: ...


@pytest.fixture
def fake_runner(monkeypatch):
    """Replace the LLM-driven runner. `hang_after` simulates a crash mid-run."""
    state: dict = {"calls": [], "hang_after": None}

    async def fake_run_experiment(client, a, b, scenarios, *, progress_cb, **_):
        state["calls"].append([s.scenario_id for s in scenarios])
        for i, scenario in enumerate(scenarios):
            if state["hang_after"] is not None and i >= state["hang_after"]:
                await asyncio.Event().wait()  # until cancelled
            await progress_cb(_comparison(scenario))

    monkeypatch.setattr(jobs, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(jobs, "LLMClient", _NullClient)
    return state


async def _experiment(db, experiment_id: str) -> ExperimentRow:
    async with db.get_sessionmaker()() as session:
        return await session.get(ExperimentRow, experiment_id)


async def _result_count(db, experiment_id: str) -> int:
    async with db.get_sessionmaker()() as session:
        return await session.scalar(
            select(func.count(ResultRow.id)).where(ResultRow.experiment_id == experiment_id)
        )


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class TestLeases:
    async def test_a_live_lease_cannot_be_taken(self, db, monkeypatch) -> None:
        await db.init_db()
        eid = await _seed_experiment(db)
        assert await jobs.claim(eid)

        monkeypatch.setattr(jobs, "WORKER_ID", "another-worker")
        assert not await jobs.claim(eid)

    async def test_a_stale_lease_is_reclaimed(self, db, monkeypatch) -> None:
        await db.init_db()
        eid = await _seed_experiment(db)
        assert await jobs.claim(eid)
        async with db.get_sessionmaker()() as session:
            await session.execute(
                update(ExperimentRow)
                .where(ExperimentRow.id == eid)
                .values(heartbeat_at=datetime.now(UTC) - timedelta(minutes=10))
            )
            await session.commit()

        monkeypatch.setattr(jobs, "WORKER_ID", "another-worker")
        assert await jobs.claim(eid)
        assert (await _experiment(db, eid)).worker_id == "another-worker"

    async def test_finished_runs_are_never_claimed(self, db) -> None:
        await db.init_db()
        eid = await _seed_experiment(db)
        async with db.get_sessionmaker()() as session:
            await session.execute(
                update(ExperimentRow)
                .where(ExperimentRow.id == eid)
                .values(status=ExperimentStatus.COMPLETED)
            )
            await session.commit()
        assert not await jobs.claim(eid)


class TestExecution:
    async def test_results_are_persisted_per_scenario(self, db, fake_runner) -> None:
        await db.init_db()
        eid = await _seed_experiment(db, limit=3)
        assert await jobs.claim(eid)
        await jobs.execute(eid)

        row = await _experiment(db, eid)
        assert row.status == ExperimentStatus.COMPLETED
        assert (row.scenario_count, row.completed_count) == (3, 3)
        assert row.worker_id is None
        assert await _result_count(db, eid) == 6

    async def test_resume_skips_scenarios_already_stored(self, db, fake_runner) -> None:
        await db.init_db()
        eid = await _seed_experiment(db, limit=3)
        first = load_eval_set()[0]
        await jobs._persist_comparison(eid, _comparison(first))

        assert await jobs.claim(eid)
        await jobs.execute(eid)

        assert first.scenario_id not in fake_runner["calls"][0]
        assert len(fake_runner["calls"][0]) == 2
        assert (await _experiment(db, eid)).completed_count == 3

    async def test_persisting_the_same_scenario_twice_writes_once(self, db) -> None:
        await db.init_db()
        eid = await _seed_experiment(db)
        comparison = _comparison(load_eval_set()[0])
        await jobs._persist_comparison(eid, comparison)
        await jobs._persist_comparison(eid, comparison)

        assert await _result_count(db, eid) == 2
        assert (await _experiment(db, eid)).completed_count == 1

    async def test_interrupted_run_keeps_its_results_and_resumes(self, db, fake_runner) -> None:
        await db.init_db()
        eid = await _seed_experiment(db, limit=3)

        # Run, let one scenario land, then shut down mid-run as a deploy would.
        fake_runner["hang_after"] = 1
        assert await jobs.launch(eid)

        async def one_stored() -> bool:
            return (await _experiment(db, eid)).completed_count == 1

        await _wait_for(one_stored)
        await jobs.shutdown()

        row = await _experiment(db, eid)
        assert row.status == ExperimentStatus.RUNNING  # unfinished, not failed
        assert row.worker_id is None  # lease handed back for immediate resumption
        assert await _result_count(db, eid) == 2

        # The next process's reaper picks it up and finishes only what is left.
        fake_runner["hang_after"] = None
        assert await jobs.reap_once() == [eid]

        async def completed() -> bool:
            return (await _experiment(db, eid)).status == ExperimentStatus.COMPLETED

        await _wait_for(completed)
        assert len(fake_runner["calls"][1]) == 2
        assert await _result_count(db, eid) == 6

    async def test_a_crashing_run_is_marked_failed(self, db, monkeypatch) -> None:
        await db.init_db()
        eid = await _seed_experiment(db)

        async def boom(*_a, **_k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(jobs, "run_experiment", boom)
        monkeypatch.setattr(jobs, "LLMClient", _NullClient)
        assert await jobs.launch(eid)

        async def failed() -> bool:
            return (await _experiment(db, eid)).status == ExperimentStatus.FAILED

        await _wait_for(failed)
        assert "provider exploded" in (await _experiment(db, eid)).error
