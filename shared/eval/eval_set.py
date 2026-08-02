"""Load the fixed eval set - the constant benchmark every prompt version is scored on."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

from shared.config import DATA_DIR
from shared.models import EvalScenario

logger = logging.getLogger(__name__)

DEFAULT_EVAL_SET = DATA_DIR / "eval_scenarios.yaml"


class EvalSetError(ValueError):
    pass


def load_eval_set(path: Path | None = None) -> list[EvalScenario]:
    path = path or DEFAULT_EVAL_SET
    if not path.exists():
        raise EvalSetError(f"Eval set not found at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "scenarios" not in raw:
        raise EvalSetError(f"{path.name}: expected a mapping with a top-level 'scenarios' key")

    scenarios = [EvalScenario.model_validate(s) for s in raw["scenarios"]]

    ids = [s.scenario_id for s in scenarios]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise EvalSetError(f"{path.name}: duplicate scenario_id(s) {sorted(duplicates)}")

    logger.info(
        "Loaded %d eval scenario(s) (%d adversarial) from %s",
        len(scenarios),
        sum(s.is_adversarial for s in scenarios),
        path.name,
    )
    return scenarios


@lru_cache
def cached_eval_set(path_str: str | None = None) -> tuple[EvalScenario, ...]:
    return tuple(load_eval_set(Path(path_str) if path_str else None))
