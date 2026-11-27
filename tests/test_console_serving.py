"""The operator console is served by the API itself, from frontend/dist."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ab_dashboard.main as main


@pytest.fixture
def client(database_url, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DUNNING_TICK_SECONDS", "0")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    dist = tmp_path / "dist"
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    with TestClient(main.app) as c:
        yield c, dist
    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()


def build(dist):
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app-1a2b.js").write_text("console.log(1)", encoding="utf-8")


def test_without_a_build_the_root_goes_to_the_api_docs(client) -> None:
    c, _ = client
    assert c.get("/", follow_redirects=False).headers["location"] == "/docs"
    assert c.get("/app/").status_code == 404


def test_client_routes_get_the_app_and_assets_are_cached(client) -> None:
    c, dist = client
    build(dist)
    assert c.get("/", follow_redirects=False).headers["location"] == "/app/"
    page = c.get("/app/customers/CUST-2000")
    assert page.text == "<div id=root></div>"
    assert page.headers["cache-control"] == "no-cache"
    asset = c.get("/app/assets/app-1a2b.js")
    assert asset.text == "console.log(1)"
    assert "max-age=31536000" in asset.headers["cache-control"]


def test_no_escape_from_the_build_directory(client) -> None:
    c, dist = client
    build(dist)
    (dist.parent / "secret.txt").write_text("nope", encoding="utf-8")
    resp = c.get("/app/..%2Fsecret.txt")
    assert "nope" not in resp.text


def test_api_routes_are_not_shadowed(client) -> None:
    c, dist = client
    build(dist)
    assert c.get("/health").json()["status"] == "ok"


def test_committed_openapi_schema_is_current() -> None:
    """The frontend's types are generated from frontend/openapi.json. If an endpoint
    changes without regenerating it, the console is type-checked against an API that
    no longer exists."""
    from ab_dashboard.export_openapi import DEFAULT_PATH, schema_text

    assert DEFAULT_PATH.read_text(encoding="utf-8") == schema_text(), (
        "frontend/openapi.json is stale: run `uv run python -m ab_dashboard.export_openapi` "
        "then `npm run gen:api` in frontend/"
    )
