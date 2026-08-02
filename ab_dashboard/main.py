"""Prompt A/B testing dashboard.

Run it:
    uv run uvicorn ab_dashboard.main:app --reload

Then drive the whole thing from http://127.0.0.1:8000/docs - no frontend required:
    1. POST /prompts/sync          import the voice agent's own prompt YAMLs
    2. POST /experiments           {"variant_a_id": "...", "variant_b_id": "..."}
    3. GET  /experiments/{id}      poll until status is "completed"
    4. GET  /experiments/{id}/results
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ab_dashboard.db import dispose_engine, init_db, normalise_database_url
from ab_dashboard.routers import experiments, prompts
from shared.config import get_settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)


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
    yield
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


@app.get("/health", tags=["meta"])
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "llm_provider_configured": settings.has_any_llm_provider,
        "voice_provider": settings.voice_provider,
    }
