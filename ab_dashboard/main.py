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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ab_dashboard import jobs
from ab_dashboard.db import dispose_engine, init_db, normalise_database_url
from ab_dashboard.routers import dunning, experiments, prompts
from shared.config import get_settings
from workflow import service as dunning_service

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)


async def _dunning_loop(interval: float) -> None:
    """The dunning scheduler: lapse promises, send due emails, escalate. Every pass is
    per-invoice transactional and safe to run on several workers at once."""
    while True:
        try:
            report = await dunning_service.tick()
            if report.emails_sent or report.escalated or report.promises_broken:
                logger.info("Dunning tick: %s", report)
        except Exception:  # noqa: BLE001 - a failed pass must not stop future passes
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

# Permissive for local dev and a static frontend on Vercel/Pages. Tighten before
# putting anything non-public behind this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(prompts.router)
app.include_router(experiments.router)
app.include_router(dunning.router)
app.include_router(dunning.tasks_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "llm_provider_configured": settings.has_any_llm_provider,
        "voice_provider": settings.voice_provider,
    }
