"""Who is acting: recorded on every audit event, task resolution and bank-line decision.

The API sets the actor from the caller's role (see ab_dashboard/auth.py); the scheduler
and the voice agent set their own. Anything else, such as a script or a test, is
"system". A context variable rather than a parameter, so the workflow's many write paths
pick it up without every function having to pass it along.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

SYSTEM = "system"
SCHEDULER = "scheduler"
VOICE_AGENT = "voice-agent"
# Follow-up tasks the workflow closes itself because the invoice was settled.
WORKFLOW = "workflow"

_actor: ContextVar[str] = ContextVar("actor", default=SYSTEM)


def current_actor() -> str:
    return _actor.get()


def set_actor(actor: str) -> None:
    """Set the actor for the rest of the current task (one API request)."""
    _actor.set(actor)


@contextmanager
def acting_as(actor: str) -> Iterator[None]:
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)
