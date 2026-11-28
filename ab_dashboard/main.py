"""Prompt A/B testing dashboard.

Run it:
    uv run uvicorn ab_dashboard.main:app --reload

Then drive the whole thing from http://127.0.0.1:8000/docs - no frontend required:
    1. POST /prompts/sync          import the voice agent's own prompt YAMLs
    2. POST /experiments           {"variant_a_id": "...", "variant_b_id": "..."}
    3. GET  /experiments/{id}      poll until status is "completed"
    4. GET  /experiments/{id}/results   (partial results are readable while it runs)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ab_dashboard import auth, jobs
from ab_dashboard import health as readiness
from ab_dashboard.db import dispose_engine, init_db, normalise_database_url
from ab_dashboard.routers import admin, cash, dunning, experiments, prompts
from shared.config import REPO_ROOT, get_settings
from shared.observability import metrics as m
from shared.observability.logging import configure_logging, request_id_var
from shared.observability.tracing import setup_tracing, tracer
from workflow import audit
from workflow import service as dunning_service

# Settings parses .env itself, but the OpenTelemetry and Langfuse variables are read from
# the real process environment (the OTel exporter reads them directly, per its spec).
# Without this, keys put in .env would be silently ignored here. Real environment
# variables still win, so a deployment's settings are never overridden by a stray file.
load_dotenv(REPO_ROOT / ".env")

_settings = get_settings()
configure_logging(_settings.log_format, _settings.log_level)
setup_tracing("ar-dashboard")
logger = logging.getLogger(__name__)


async def _dunning_loop(interval: float) -> None:
    """The dunning scheduler: lapse promises, send due emails, escalate. Every pass is
    per-invoice transactional and safe to run on several workers at once."""
    state = readiness.scheduler_state
    audit.set_actor(audit.SCHEDULER)  # this task's audit events are the scheduler's
    while True:
        try:
            report = await dunning_service.tick()
            state.last_success, state.last_error = time.time(), None
            if report.emails_sent or report.escalated or report.promises_broken:
                logger.info("Dunning tick: %s", report)
        except Exception as exc:  # noqa: BLE001 - a failed pass must not stop future passes
            state.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Dunning tick failed", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    url = normalise_database_url(settings.database_url)
    logger.info("Database: %s", url.split("@")[-1])  # never log credentials
    await init_db()
    if not settings.has_any_llm_provider:
        logger.warning(
            "No GROQ_API_KEY or CEREBRAS_API_KEY set - experiments will be rejected. "
            "Copy .env.example to .env and fill one in."
        )
    # Resumes runs interrupted by the last restart, then keeps sweeping for runs whose
    # worker died without releasing them.
    background = [asyncio.create_task(jobs.reaper_loop(), name="experiment-reaper")]
    if settings.dunning_tick_seconds > 0:
        background.append(
            asyncio.create_task(
                _dunning_loop(settings.dunning_tick_seconds), name="dunning-scheduler"
            )
        )
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await jobs.shutdown()
        await dispose_engine()


app = FastAPI(
    title="Prompt A/B Testing Dashboard",
    description=__doc__,
    version="0.1.0",
    lifespan=lifespan,
)

# The console is served from this origin, so it needs no CORS. Other browser origins
# (a frontend dev server on another host, a separate site) must be listed explicitly in
# CORS_ALLOW_ORIGINS: with API keys in play, "*" would let any page a signed-in user
# visits call the API.
_origins = [o.strip() for o in _settings.cors_allow_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type", "X-API-Key", "Authorization", "X-Act-As", "X-Request-ID"],
    )


@app.middleware("http")
async def observe_request(request: Request, call_next):
    """Request id, server span and HTTP metrics for every request.

    The id is taken from X-Request-ID when a proxy set one, so a request can be followed
    across services; otherwise generated. It is echoed back and stamped on every log line
    the request produces.
    """
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    status_code = 500
    with tracer.start_as_current_span(f"HTTP {request.method}") as span:
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("request.id", request_id)
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # The route *template* (/tasks/{task_id}), never the raw path: one series per
            # endpoint rather than one per task id. Unmatched paths share a single label.
            route = request.scope.get("route")
            label = getattr(route, "path", None) or "unmatched"
            span.update_name(f"HTTP {request.method} {label}")
            span.set_attribute("http.route", label)
            span.set_attribute("http.response.status_code", status_code)
            m.HTTP_REQUESTS.labels(request.method, label, str(status_code)).inc()
            m.HTTP_LATENCY.labels(request.method, label).observe(time.perf_counter() - started)
            request_id_var.reset(token)


@app.get("/metrics", tags=["meta"], include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. Workflow gauges are recomputed from the database on
    each scrape, so every worker reports the same, current state."""
    try:
        await dunning_service.refresh_metrics()
    except Exception:  # noqa: BLE001 - a DB blip must not take the metrics endpoint down
        logger.warning("Could not refresh workflow gauges", exc_info=True)
    return Response(generate_latest(m.REGISTRY), media_type=CONTENT_TYPE_LATEST)


app.include_router(auth.router)
app.include_router(prompts.router)
app.include_router(experiments.router)
app.include_router(dunning.router)
app.include_router(dunning.tasks_router)
app.include_router(cash.router)
app.include_router(admin.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, object]:
    """Liveness: the process is up and serving. Cheap; touches nothing."""
    settings = get_settings()
    return {
        "status": "ok",
        "llm_provider_configured": settings.has_any_llm_provider,
        "voice_provider": settings.voice_provider,
    }


@app.get("/health/ready", tags=["meta"])
async def ready(response: Response) -> dict[str, object]:
    """Readiness: database reachable, schema at head, scheduler alive. 503 if not."""
    settings = get_settings()
    checks = {
        "database": await readiness.check_database(),
        "schema": await readiness.check_schema(),
        "scheduler": readiness.check_scheduler(settings.dunning_tick_seconds),
    }
    is_ready = all(c.ok for c in checks.values())
    if not is_ready:
        response.status_code = 503
    return {
        "status": "ready" if is_ready else "not_ready",
        "checks": {name: c.__dict__ for name, c in checks.items()},
        "llm_provider_configured": settings.has_any_llm_provider,
    }


# --------------------------------------------------------------------------------------
# Operator console (frontend/), served from the same origin as the API
# --------------------------------------------------------------------------------------

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/app/" if (FRONTEND_DIST / "index.html").is_file() else "/docs")


@app.get("/app/{path:path}", include_in_schema=False)
async def console(path: str) -> FileResponse:
    """The built single-page app. A real file (a hashed asset) is served as is; any
    other path is a client-side route and gets index.html."""
    root_dir = FRONTEND_DIST.resolve()
    index = root_dir / "index.html"
    if not index.is_file():
        raise HTTPException(404, "Frontend not built: cd frontend && npm ci && npm run build")
    candidate = (root_dir / path).resolve()
    if path and candidate.is_file() and root_dir in candidate.parents:
        # Vite fingerprints asset names, so they can be cached for good.
        return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000"})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
