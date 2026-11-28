"""Who may do what through the API.

Three roles, each including the one before it:

    viewer    read everything                       (no key, on a deployed demo)
    operator  work the book: record outcomes and payments, resolve tasks, run the
              scheduler, apply or dismiss bank exceptions, sync prompts
    admin     also: write a debt off or send it to legal, start an experiment (it spends
              LLM budget), register a prompt, reset the demo

A key is sent as `X-API-Key` (or `Authorization: Bearer`). A wrong key is a 401, never a
silent fall back to the public role. `X-Act-As` lets a signed-in user act with a lower
role, so a demo can show what an operator or a visitor sees without a second key; it can
never raise a role.

The role is also the actor written to the audit trail (workflow/audit.py).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from shared.config import Settings, get_settings
from workflow import audit


class Role(IntEnum):
    VIEWER = 0
    OPERATOR = 1
    ADMIN = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls[value.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown role {value!r}") from None


@dataclass(frozen=True)
class Principal:
    role: Role
    # Recorded on the audit trail: "admin", "operator", "admin as operator", "visitor"
    # (no key, on a deployment that grants the public a role), or "local" (no keys
    # configured at all).
    actor: str
    authenticated: bool


def auth_enabled(settings: Settings) -> bool:
    return bool(settings.operator_api_key or settings.admin_api_key)


def public_role(settings: Settings) -> Role:
    if settings.public_role:
        return Role.parse(settings.public_role)
    return Role.VIEWER if auth_enabled(settings) else Role.ADMIN


def _presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if not key:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        key = token if scheme.lower() == "bearer" else None
    return key.strip() if key and key.strip() else None


def _matches(given: str, expected: str | None) -> bool:
    # Constant time, so response timing does not leak how much of a guess was right.
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def identify(request: Request, settings: Settings | None = None) -> Principal:
    s = settings or get_settings()
    key = _presented_key(request)
    if key is not None:
        if _matches(key, s.admin_api_key):
            principal = Principal(Role.ADMIN, "admin", True)
        elif _matches(key, s.operator_api_key):
            principal = Principal(Role.OPERATOR, "operator", True)
        else:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "That API key is not valid.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        role = public_role(s)
        principal = Principal(role, "visitor" if auth_enabled(s) else "local", False)

    act_as = request.headers.get("x-act-as")
    if act_as:
        try:
            lower = Role.parse(act_as)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if lower > principal.role:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"X-Act-As can only lower a role; you are {principal.role.label}.",
            )
        if lower < principal.role:
            principal = Principal(
                lower, f"{principal.actor} as {lower.label}", principal.authenticated
            )
    return principal


async def current_principal(request: Request) -> Principal:
    """Who is calling. Also makes them the actor for everything this request writes."""
    principal = identify(request)
    audit.set_actor(principal.actor)
    return principal


def require(role: Role):
    """A route dependency: the caller needs at least `role`."""

    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        ensure(principal, role)
        return principal

    return dependency


def ensure(principal: Principal, role: Role) -> None:
    if principal.role >= role:
        return
    if principal.authenticated:
        message = f"This needs the {role.label} role; you are acting as {principal.role.label}."
    else:
        message = f"Sign in with an {role.label} key to do this."
    raise HTTPException(status.HTTP_403_FORBIDDEN, message)


OPERATOR = Depends(require(Role.OPERATOR))
ADMIN = Depends(require(Role.ADMIN))


# --------------------------------------------------------------------------------------
# GET /auth/me
# --------------------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


class MeOut(BaseModel):
    role: Literal["viewer", "operator", "admin"]
    actor: str = Field(description="What the audit trail records for this caller")
    authenticated: bool
    auth_enabled: bool = Field(description="False on a checkout with no keys configured")
    public_role: Literal["viewer", "operator", "admin"] = Field(
        description="What a visitor without a key can do"
    )
    can_act_as: list[Literal["viewer", "operator", "admin"]] = Field(
        description="Roles this caller may switch to with X-Act-As"
    )


@router.get("/me", response_model=MeOut)
async def me(request: Request) -> MeOut:
    """Who the API thinks you are. A wrong key is a 401, so this also checks a key."""
    settings = get_settings()
    principal = identify(request, settings)
    # The role before any X-Act-As, which is what bounds the switcher.
    real = identify(_without_act_as(request), settings).role
    return MeOut(
        role=principal.role.label,
        actor=principal.actor,
        authenticated=principal.authenticated,
        auth_enabled=auth_enabled(settings),
        public_role=public_role(settings).label,
        can_act_as=[r.label for r in Role if r <= real],
    )


def _without_act_as(request: Request) -> Request:
    scope = dict(request.scope)
    scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"x-act-as"]
    return Request(scope)
