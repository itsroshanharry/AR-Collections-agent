"""Database portability and the HTTP surface.

SQLite locally and Postgres deployed means the model set must stay in the portable
intersection. Set TEST_POSTGRES_URL to run the same round-trip against a real Postgres;
without it those tests skip rather than give false assurance.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import ARRAY, JSON, inspect

from ab_dashboard.db import (
    Base,
    CallLogRow,
    ExperimentRow,
    PromptVersionRow,
    ResultRow,
    normalise_database_url,
)


class TestUrlNormalisation:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            # Neon and Render both hand out this form; SQLAlchemy rejects it outright.
            ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
            # This form silently selects the SYNC driver and deadlocks the event loop.
            ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
            ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
            ("sqlite:///./local.db", "sqlite+aiosqlite:///./local.db"),
            ("sqlite+aiosqlite:///./local.db", "sqlite+aiosqlite:///./local.db"),
        ],
    )
    def test_coerces_to_an_async_driver(self, given: str, expected: str) -> None:
        assert normalise_database_url(given) == expected

    def test_preserves_query_parameters(self) -> None:
        url = normalise_database_url("postgres://u:p@host/db?sslmode=require")
        assert url.endswith("?sslmode=require")
        assert url.startswith("postgresql+psycopg://")


class TestDialectPortability:
    """Guards against a Postgres-only construct sneaking in and breaking local dev
    (or vice versa). Cheaper than discovering it at deploy time."""

    def test_no_postgres_only_column_types(self) -> None:
        for table in Base.metadata.tables.values():
            for column in table.columns:
                assert not isinstance(column.type, ARRAY), (
                    f"{table.name}.{column.name} uses ARRAY, which SQLite cannot store"
                )
                type_name = type(column.type).__name__
                assert type_name != "JSONB", (
                    f"{table.name}.{column.name} uses JSONB; use portable JSON instead"
                )

    def test_json_columns_use_the_generic_type(self) -> None:
        json_columns = [
            (t.name, c.name)
            for t in Base.metadata.tables.values()
            for c in t.columns
            if isinstance(c.type, JSON)
        ]
        assert json_columns, "expected JSON columns for scores and figure lists"

    def test_expected_tables_exist(self) -> None:
        assert set(Base.metadata.tables) == {
            "prompt_versions",
            "experiments",
            "experiment_results",
            "call_logs",
        }

    def test_pairwise_verdict_is_persisted(self) -> None:
        """Without these columns /results can only fall back to comparing absolute
        composites, discarding the position-blind judgement entirely."""
        columns = {c.name for c in ResultRow.__table__.columns}
        assert {"pairwise_winner", "pairwise_reason", "presented_a_first"} <= columns

    def test_results_cascade_from_experiments(self) -> None:
        fk = next(iter(ResultRow.__table__.c.experiment_id.foreign_keys))
        assert fk.ondelete == "CASCADE"


@pytest.fixture
async def session_factory(tmp_path):
    """A throwaway SQLite database per test."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestRoundTrip:
    async def test_prompt_version_round_trips_with_json_list(self, session_factory) -> None:
        async with session_factory() as session:
            session.add(
                PromptVersionRow(
                    id="p1",
                    name="agent",
                    version=1,
                    tone="firm",
                    template="hello",
                    applies_to_tier=["high", "critical"],
                )
            )
            await session.commit()
        async with session_factory() as session:
            row = await session.get(PromptVersionRow, "p1")
            assert row.applies_to_tier == ["high", "critical"]

    async def test_result_round_trips_scores_dict(self, session_factory) -> None:
        async with session_factory() as session:
            experiment = ExperimentRow(variant_a_id="a", variant_b_id="b")
            session.add(experiment)
            await session.flush()
            session.add(
                ResultRow(
                    experiment_id=experiment.id,
                    scenario_id="S1",
                    variant_label="A",
                    prompt_id="a",
                    scores={"on_topic": 5, "compliant": 4},
                    hallucinated_figures=["7500"],
                    pairwise_winner="A",
                    presented_a_first=False,
                )
            )
            await session.commit()
            experiment_id = experiment.id

        async with session_factory() as session:
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(ResultRow).where(ResultRow.experiment_id == experiment_id)
                )
            ).scalar_one()
            assert row.scores == {"on_topic": 5, "compliant": 4}
            assert row.hallucinated_figures == ["7500"]
            assert row.pairwise_winner == "A"
            assert row.presented_a_first is False

    async def test_call_log_round_trips(self, session_factory) -> None:
        async with session_factory() as session:
            session.add(
                CallLogRow(
                    invoice_id="INV-1",
                    customer_id="C-1",
                    prompt_id="p1",
                    outcome="promise_to_pay",
                    promise_to_pay_date="2026-08-08",
                    composite=4.4,
                    scores={"on_topic": 5},
                )
            )
            await session.commit()
        async with session_factory() as session:
            from sqlalchemy import select

            row = (await session.execute(select(CallLogRow))).scalar_one()
            assert row.promise_to_pay_date == "2026-08-08"
            assert row.composite == pytest.approx(4.4)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="Set TEST_POSTGRES_URL to run the Postgres dialect check",
)
class TestPostgresDialect:
    async def test_schema_creates_and_round_trips_on_postgres(self) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(normalise_database_url(os.environ["TEST_POSTGRES_URL"]))
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
                tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            assert "experiment_results" in tables

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(
                    PromptVersionRow(
                        id="p1",
                        name="agent",
                        version=1,
                        tone="firm",
                        template="x",
                        applies_to_tier=["high"],
                    )
                )
                await session.commit()
            async with factory() as session:
                row = await session.get(PromptVersionRow, "p1")
                assert row.applies_to_tier == ["high"]
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await engine.dispose()


class TestApi:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
        from shared.config import get_settings

        get_settings.cache_clear()

        import ab_dashboard.db as db_module

        db_module._engine = None
        db_module._sessionmaker = None

        from fastapi.testclient import TestClient

        from ab_dashboard.main import app

        with TestClient(app) as client:
            yield client

        db_module._engine = None
        db_module._sessionmaker = None
        get_settings.cache_clear()

    def test_health(self, client) -> None:
        assert client.get("/health").json()["status"] == "ok"

    def test_sync_imports_the_agents_own_prompts(self, client) -> None:
        body = client.post("/prompts/sync").json()
        assert "collections_formal_v1" in body["imported"]
        assert len(body["imported"]) >= 3

    def test_sync_is_idempotent(self, client) -> None:
        client.post("/prompts/sync")
        body = client.post("/prompts/sync").json()
        assert body["imported"] == []
        assert len(body["updated"]) >= 3

    def test_history_returns_the_full_lineage(self, client) -> None:
        client.post("/prompts/sync")
        body = client.get("/prompts/collections_formal_v1/history").json()
        assert [p["version"] for p in body] == sorted(p["version"] for p in body)
        assert len({p["name"] for p in body}) == 1

    def test_experiment_rejects_identical_variants(self, client) -> None:
        client.post("/prompts/sync")
        r = client.post(
            "/experiments",
            json={
                "variant_a_id": "collections_formal_v1",
                "variant_b_id": "collections_formal_v1",
            },
        )
        assert r.status_code == 400

    def test_experiment_rejects_unknown_prompt(self, client) -> None:
        client.post("/prompts/sync")
        r = client.post(
            "/experiments",
            json={"variant_a_id": "ghost", "variant_b_id": "collections_formal_v1"},
        )
        assert r.status_code == 404

    def test_unknown_prompt_returns_404(self, client) -> None:
        assert client.get("/prompts/ghost").status_code == 404

    def test_unknown_experiment_returns_404(self, client) -> None:
        assert client.get("/experiments/ghost").status_code == 404
        assert client.get("/experiments/ghost/results").status_code == 404
