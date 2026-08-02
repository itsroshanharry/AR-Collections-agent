"""Load and validate versioned prompt templates from YAML.

Deliberately shared by both sub-projects: the voice agent runs these prompts, and the
A/B dashboard imports the same files via POST /prompts/sync. That means an experiment
compares the agent's actual production prompts, not a re-typed approximation of them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import yaml

from shared.config import PROMPTS_DIR
from shared.models import PromptVersion

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {"id", "name", "version", "tone", "template"}

# Placeholders PromptVersion.render() supplies. A template referencing anything else
# would raise KeyError at call time, so we reject it at load time instead.
KNOWN_PLACEHOLDERS = {
    "invoice_context",
    "customer_name",
    "contact_name",
    "amount_due",
    "invoice_number",
    "days_overdue",
    "due_date",
}


class PromptValidationError(ValueError):
    pass


def _validate_placeholders(prompt: PromptVersion, source: Path) -> None:
    import string

    used = {
        field
        for _, field, _, _ in string.Formatter().parse(prompt.template)
        if field is not None
    }
    unknown = used - KNOWN_PLACEHOLDERS
    if unknown:
        raise PromptValidationError(
            f"{source.name}: template uses unknown placeholder(s) {sorted(unknown)}. "
            f"Known: {sorted(KNOWN_PLACEHOLDERS)}"
        )


def load_prompt_file(path: Path) -> PromptVersion:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PromptValidationError(f"{path.name}: expected a YAML mapping")

    missing = REQUIRED_FIELDS - raw.keys()
    if missing:
        raise PromptValidationError(f"{path.name}: missing required field(s) {sorted(missing)}")

    prompt = PromptVersion.model_validate(raw)
    _validate_placeholders(prompt, path)
    return prompt


def iter_prompt_files(directory: Path | None = None) -> Iterator[Path]:
    directory = directory or PROMPTS_DIR
    if not directory.exists():
        return
    yield from sorted(directory.glob("*.yaml"))
    yield from sorted(directory.glob("*.yml"))


def load_all(directory: Path | None = None) -> list[PromptVersion]:
    """Load every prompt in a directory, failing loudly on duplicates.

    Duplicate ids are an error rather than a last-write-wins: silently shadowing a
    prompt would make an experiment's recorded version id a lie.
    """
    prompts: list[PromptVersion] = []
    seen: dict[str, Path] = {}
    for path in iter_prompt_files(directory):
        prompt = load_prompt_file(path)
        if prompt.id in seen:
            raise PromptValidationError(
                f"Duplicate prompt id {prompt.id!r} in {path.name} and {seen[prompt.id].name}"
            )
        seen[prompt.id] = path
        prompts.append(prompt)
    logger.info("Loaded %d prompt version(s) from %s", len(prompts), directory or PROMPTS_DIR)
    return prompts


def load_by_id(prompt_id: str, directory: Path | None = None) -> PromptVersion:
    for prompt in load_all(directory):
        if prompt.id == prompt_id:
            return prompt
    raise KeyError(f"No prompt version with id {prompt_id!r}")


def history(name: str, directory: Path | None = None) -> list[PromptVersion]:
    """All versions of a named prompt, oldest first."""
    return sorted(
        (p for p in load_all(directory) if p.name == name),
        key=lambda p: p.version,
    )
