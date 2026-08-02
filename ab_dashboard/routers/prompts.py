"""Prompt version registry and lineage."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ab_dashboard.db import PromptVersionRow, get_session
from shared.models import RiskTier, Tone
from shared.prompt_store import PromptValidationError, load_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prompts", tags=["prompts"])


class PromptIn(BaseModel):
    id: str = Field(examples=["collections_formal_v1"])
    name: str = Field(examples=["collections_agent"])
    version: int = Field(ge=1, examples=[1])
    tone: Tone
    template: str
    changelog: str = ""
    applies_to_tier: list[RiskTier] = Field(default_factory=list)


class PromptOut(BaseModel):
    id: str
    name: str
    version: int
    tone: str
    template: str
    changelog: str
    applies_to_tier: list[str]

    @classmethod
    def from_row(cls, row: PromptVersionRow) -> PromptOut:
        return cls(
            id=row.id,
            name=row.name,
            version=row.version,
            tone=row.tone,
            template=row.template,
            changelog=row.changelog,
            applies_to_tier=list(row.applies_to_tier or []),
        )


class SyncResult(BaseModel):
    imported: list[str]
    updated: list[str]
    skipped: list[str]


async def _upsert(session: AsyncSession, data: PromptIn) -> tuple[PromptVersionRow, bool]:
    """Returns (row, created)."""
    existing = await session.get(PromptVersionRow, data.id)
    tiers = [t.value for t in data.applies_to_tier]
    if existing:
        existing.name = data.name
        existing.version = data.version
        existing.tone = data.tone.value
        existing.template = data.template
        existing.changelog = data.changelog
        existing.applies_to_tier = tiers
        return existing, False

    row = PromptVersionRow(
        id=data.id,
        name=data.name,
        version=data.version,
        tone=data.tone.value,
        template=data.template,
        changelog=data.changelog,
        applies_to_tier=tiers,
    )
    session.add(row)
    return row, True


@router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
async def register_prompt(
    data: PromptIn, session: AsyncSession = Depends(get_session)
) -> PromptOut:
    """Register (or update) a prompt version."""
    row, _ = await _upsert(session, data)
    await session.commit()
    return PromptOut.from_row(row)


@router.post("/sync", response_model=SyncResult)
async def sync_from_disk(session: AsyncSession = Depends(get_session)) -> SyncResult:
    """Import the voice agent's own prompt YAMLs.

    This is what makes an experiment meaningful: the dashboard tests the exact prompts
    the agent runs in production, loaded through the same validator, rather than a
    re-typed copy that can silently drift.
    """
    try:
        prompts = load_all()
    except PromptValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    imported, updated = [], []
    for prompt in prompts:
        _, created = await _upsert(
            session,
            PromptIn(
                id=prompt.id,
                name=prompt.name,
                version=prompt.version,
                tone=prompt.tone,
                template=prompt.template,
                changelog=prompt.changelog,
                applies_to_tier=prompt.applies_to_tier,
            ),
        )
        (imported if created else updated).append(prompt.id)

    await session.commit()
    logger.info(
        "Synced %d prompt(s): %d new, %d updated", len(prompts), len(imported), len(updated)
    )
    return SyncResult(imported=imported, updated=updated, skipped=[])


@router.get("", response_model=list[PromptOut])
async def list_prompts(session: AsyncSession = Depends(get_session)) -> list[PromptOut]:
    rows = (
        await session.execute(
            select(PromptVersionRow).order_by(
                PromptVersionRow.name, PromptVersionRow.version
            )
        )
    ).scalars()
    return [PromptOut.from_row(r) for r in rows]


@router.get("/{prompt_id}", response_model=PromptOut)
async def get_prompt(
    prompt_id: str, session: AsyncSession = Depends(get_session)
) -> PromptOut:
    row = await session.get(PromptVersionRow, prompt_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No prompt {prompt_id!r}")
    return PromptOut.from_row(row)


@router.get("/{prompt_id}/history", response_model=list[PromptOut])
async def prompt_history(
    prompt_id: str, session: AsyncSession = Depends(get_session)
) -> list[PromptOut]:
    """Every version sharing this prompt's lineage name, oldest first."""
    row = await session.get(PromptVersionRow, prompt_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No prompt {prompt_id!r}")
    rows = (
        await session.execute(
            select(PromptVersionRow)
            .where(PromptVersionRow.name == row.name)
            .order_by(PromptVersionRow.version)
        )
    ).scalars()
    return [PromptOut.from_row(r) for r in rows]
