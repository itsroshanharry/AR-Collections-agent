"""Administration: actions that only an admin may take on a running demo."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from ab_dashboard.auth import ADMIN
from workflow import demo, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[ADMIN])


class ResetOut(BaseModel):
    deleted: dict[str, int]
    reloaded: service.SyncReport | None


@router.post("/demo/reset", response_model=ResetOut)
async def reset_demo(
    reload: bool = Query(default=True, description="Reload the ledger after wiping"),
) -> ResetOut:
    """Wipe the workflow and cash application and reload the ledger, putting a demo that
    visitors have clicked through back to a clean start. Prompts and experiment results
    are kept."""
    try:
        report = await demo.reset(reload=reload)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    logger.warning("Demo reset: deleted %s", report.deleted)
    return ResetOut(deleted=report.deleted, reloaded=report.reloaded)
