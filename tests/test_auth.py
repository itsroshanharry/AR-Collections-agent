"""Roles and keys: who may read, who may act, and what the audit trail records."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from ab_dashboard import auth
from ab_dashboard.auth import Role
from shared.config import Settings
from tests.test_workflow_service import medium

OPERATOR_KEY = "op-key-123"
ADMIN_KEY = "admin-key-456"


def request(**headers: str) -> Request:
    raw = [(k.lower().replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **{"operator_api_key": OPERATOR_KEY, "admin_api_key": ADMIN_KEY, **overrides},
    )


class TestIdentify:
    def test_no_keys_configured_means_local_admin(self) -> None:
        p = auth.identify(request(), Settings(_env_file=None))
        assert (p.role, p.actor, p.authenticated) == (Role.ADMIN, "local", False)

    def test_with_keys_a_visitor_is_a_viewer(self) -> None:
        p = auth.identify(request(), settings())
        assert (p.role, p.actor) == (Role.VIEWER, "visitor")

    def test_public_role_can_be_granted(self) -> None:
        assert auth.identify(request(), settings(public_role="operator")).role is Role.OPERATOR

    def test_public_role_viewer_fails_closed_without_keys(self) -> None:
        s = Settings(_env_file=None, public_role="viewer")
        assert auth.identify(request(), s).role is Role.VIEWER

    def test_keys_select_roles(self) -> None:
        s = settings()
        assert auth.identify(request(x_api_key=OPERATOR_KEY), s).role is Role.OPERATOR
        assert auth.identify(request(x_api_key=ADMIN_KEY), s).role is Role.ADMIN
        bearer = auth.identify(request(authorization=f"Bearer {ADMIN_KEY}"), s)
        assert bearer.role is Role.ADMIN

    def test_wrong_key_is_rejected_not_downgraded(self) -> None:
        with pytest.raises(auth.HTTPException) as exc:
            auth.identify(request(x_api_key="guess"), settings())
        assert exc.value.status_code == 401

    def test_act_as_can_only_lower(self) -> None:
        s = settings()
        p = auth.identify(request(x_api_key=ADMIN_KEY, x_act_as="operator"), s)
        assert (p.role, p.actor) == (Role.OPERATOR, "admin as operator")
        with pytest.raises(auth.HTTPException) as exc:
            auth.identify(request(x_api_key=OPERATOR_KEY, x_act_as="admin"), s)
        assert exc.value.status_code == 403

    def test_blank_env_values_mean_unset(self) -> None:
        s = Settings(_env_file=None, admin_api_key="  ", public_role="")
        assert (s.admin_api_key, s.public_role) == (None, None)


@pytest.fixture
def client(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    monkeypatch.setenv("OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    import ab_dashboard.db as db_module
    import data.generate_dataset as dataset
    from shared.config import get_settings

    monkeypatch.setattr(
        dataset, "load_invoices", lambda: [medium(dispute_flag=True), medium("INV-2", "C-2")]
    )
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


OP = {"X-API-Key": OPERATOR_KEY}
ADMIN = {"X-API-Key": ADMIN_KEY}


class TestApi:
    def test_me_reports_the_role(self, client) -> None:
        assert client.get("/auth/me").json()["role"] == "viewer"
        body = client.get("/auth/me", headers=ADMIN).json()
        assert (body["role"], body["authenticated"], body["auth_enabled"]) == ("admin", True, True)
        assert body["can_act_as"] == ["viewer", "operator", "admin"]
        acting = client.get("/auth/me", headers={**ADMIN, "X-Act-As": "viewer"}).json()
        assert acting["role"] == "viewer"
        assert acting["can_act_as"] == ["viewer", "operator", "admin"]
        assert client.get("/auth/me", headers={"X-API-Key": "nope"}).status_code == 401

    def test_visitors_read_but_cannot_write(self, client) -> None:
        assert client.get("/dunning/summary").status_code == 200
        assert client.get("/tasks").status_code == 200
        r = client.post("/dunning/sync")
        assert r.status_code == 403
        assert "Sign in" in r.json()["detail"]

    def test_operator_works_the_book_and_is_recorded(self, client) -> None:
        assert client.post("/dunning/sync", headers=OP).status_code == 200
        [task] = client.get("/tasks", params={"invoice_id": "INV-M"}).json()
        r = client.patch(f"/tasks/{task['id']}", json={"resolution": "rejected"}, headers=OP)
        assert r.status_code == 200
        assert r.json()["resolved_by"] == "operator"
        timeline = client.get("/dunning/invoices/INV-M").json()["timeline"]
        actors = {e["event"]: e["actor"] for e in timeline}
        assert actors["task:review_dispute:rejected"] == "operator"
        assert set(actors.values()) == {"operator"}  # the sync too

    def test_writing_off_needs_admin(self, client) -> None:
        client.post("/dunning/sync", headers=OP)
        # write_off belongs to escalation reviews, but the role check comes first, so
        # the dispute task already open on INV-M is enough to exercise it.
        [task] = client.get("/tasks", params={"invoice_id": "INV-M"}).json()
        r = client.patch(f"/tasks/{task['id']}", json={"resolution": "write_off"}, headers=OP)
        assert r.status_code == 403
        assert "admin" in r.json()["detail"]

    def test_experiments_and_prompt_registration_need_admin(self, client) -> None:
        assert client.post("/prompts/sync", headers=OP).status_code == 200
        body = {"variant_a_id": "collections_formal_v1", "variant_b_id": "collections_friendly_v2"}
        assert client.post("/experiments", json=body, headers=OP).status_code == 403
        assert client.post("/prompts", json={}, headers=OP).status_code == 403

    def test_act_as_viewer_blocks_an_admin(self, client) -> None:
        r = client.post("/dunning/sync", headers={**ADMIN, "X-Act-As": "viewer"})
        assert r.status_code == 403
        assert "acting as viewer" in r.json()["detail"]

    def test_health_and_metrics_stay_open(self, client) -> None:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_cors_is_off_unless_origins_are_listed(client) -> None:
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


class TestDemoReset:
    def test_reset_needs_admin_and_restores_a_clean_book(self, client) -> None:
        client.post("/dunning/sync", headers=OP)
        [task] = client.get("/tasks", params={"invoice_id": "INV-M"}).json()
        client.patch(f"/tasks/{task['id']}", json={"resolution": "rejected"}, headers=OP)
        client.post(
            "/cash/transactions",
            json=[
                {"utr": "U1", "value_date": "2026-11-24", "amount": "50000", "reference": "INV-2"}
            ],
            headers=OP,
        )

        assert client.post("/admin/demo/reset", headers=OP).status_code == 403
        body = client.post("/admin/demo/reset", headers=ADMIN).json()
        assert body["deleted"]["invoice_states"] == 2
        assert body["deleted"]["bank_transactions"] == 1
        assert body["reloaded"]["created"] == 2

        # Back to how the ledger arrives: the dispute task is open again, no cash.
        [fresh] = client.get("/tasks", params={"invoice_id": "INV-M"}).json()
        assert fresh["status"] == "open"
        assert client.get("/cash/transactions").json() == []
        assert client.get("/dunning/invoices/INV-2").json()["state"]["state"] != "closed"

    def test_reset_without_reload_leaves_it_empty(self, client) -> None:
        client.post("/dunning/sync", headers=OP)
        body = client.post("/admin/demo/reset", params={"reload": False}, headers=ADMIN).json()
        assert body["reloaded"] is None
        assert client.get("/dunning/summary").json()["invoices"] == 0
