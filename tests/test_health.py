"""Liveness and readiness checks."""

from __future__ import annotations

import pytest

from ab_dashboard import health


@pytest.fixture
def client(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    from fastapi.testclient import TestClient

    from ab_dashboard.main import app

    with TestClient(app) as c:
        yield c
    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()


def test_ready_when_database_and_schema_are_good(client) -> None:
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["schema"]["detail"].startswith("at head")


def test_not_ready_when_schema_is_behind(client, monkeypatch) -> None:
    async def behind():
        return health.Check(False, "schema at 0003, code expects 0004")

    monkeypatch.setattr(health, "check_schema", behind)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


def test_liveness_stays_up_regardless(client, monkeypatch) -> None:
    async def down():
        return health.Check(False, "OperationalError")

    monkeypatch.setattr(health, "check_database", down)
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 503


class TestScheduler:
    @pytest.fixture(autouse=True)
    def fresh_state(self, monkeypatch):
        monkeypatch.setattr(health, "scheduler_state", health.SchedulerState(started_at=1000.0))

    def test_disabled_is_fine(self) -> None:
        assert health.check_scheduler(0).ok

    def test_grace_period_after_start(self) -> None:
        assert health.check_scheduler(60, now=1100.0).ok

    def test_never_completing_a_pass_fails(self) -> None:
        health.scheduler_state.last_error = "OperationalError: locked"
        check = health.check_scheduler(60, now=1000.0 + 60 * 3 + 1)
        assert not check.ok
        assert "locked" in check.detail

    def test_recent_pass_is_fine(self) -> None:
        health.scheduler_state.last_success = 2000.0
        assert health.check_scheduler(60, now=2050.0).ok

    def test_stalled_scheduler_fails(self) -> None:
        health.scheduler_state.last_success = 2000.0
        assert not health.check_scheduler(60, now=2000.0 + 181).ok
