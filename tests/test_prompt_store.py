"""Prompt loading and validation.

The store is shared by the agent and the dashboard, so a bad prompt must fail at load
time. Failing later - at call time, mid-conversation - would surface as a dropped call.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from shared.config import PROMPTS_DIR
from shared.models import RiskTier, Tone
from shared.prompt_store import (
    PromptValidationError,
    history,
    load_all,
    load_by_id,
    load_prompt_file,
)
from tests.conftest import make_invoice

VALID = """\
id: test_v1
name: test_agent
version: 1
tone: firm
applies_to_tier: [high]
template: |
  Invoice {invoice_number} for {amount_due}.
  {invoice_context}
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestValidation:
    def test_loads_a_valid_prompt(self, tmp_path: Path) -> None:
        prompt = load_prompt_file(_write(tmp_path, "a.yaml", VALID))
        assert prompt.id == "test_v1"
        assert prompt.tone is Tone.FIRM
        assert prompt.applies_to_tier == [RiskTier.HIGH]

    def test_rejects_missing_required_field(self, tmp_path: Path) -> None:
        content = VALID.replace("tone: firm\n", "")
        with pytest.raises(PromptValidationError, match="missing required field"):
            load_prompt_file(_write(tmp_path, "a.yaml", content))

    def test_rejects_unknown_placeholder(self, tmp_path: Path) -> None:
        """A typo'd placeholder would raise KeyError mid-call; catch it at load."""
        content = VALID.replace("{invoice_number}", "{invoce_number}")
        with pytest.raises(PromptValidationError, match="unknown placeholder"):
            load_prompt_file(_write(tmp_path, "a.yaml", content))

    def test_rejects_non_mapping_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(PromptValidationError, match="expected a YAML mapping"):
            load_prompt_file(_write(tmp_path, "a.yaml", "- just\n- a list\n"))

    def test_rejects_duplicate_ids_rather_than_shadowing(self, tmp_path: Path) -> None:
        """Last-write-wins would make a recorded prompt version id a lie."""
        _write(tmp_path, "a.yaml", VALID)
        _write(tmp_path, "b.yaml", VALID.replace("version: 1", "version: 2"))
        with pytest.raises(PromptValidationError, match="Duplicate prompt id"):
            load_all(tmp_path)

    def test_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert load_all(tmp_path) == []


class TestLookup:
    def test_load_by_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", VALID)
        assert load_by_id("test_v1", tmp_path).version == 1

    def test_load_by_id_raises_for_unknown(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", VALID)
        with pytest.raises(KeyError):
            load_by_id("nope", tmp_path)

    def test_history_is_ordered_oldest_first(self, tmp_path: Path) -> None:
        _write(tmp_path, "c.yaml", VALID.replace("id: test_v1", "id: test_v3").replace(
            "version: 1", "version: 3"
        ))
        _write(tmp_path, "a.yaml", VALID)
        _write(tmp_path, "b.yaml", VALID.replace("id: test_v1", "id: test_v2").replace(
            "version: 1", "version: 2"
        ))
        assert [p.version for p in history("test_agent", tmp_path)] == [1, 2, 3]


class TestRendering:
    def test_render_substitutes_invoice_facts(self, tmp_path: Path) -> None:
        prompt = load_prompt_file(_write(tmp_path, "a.yaml", VALID))
        rendered = prompt.render(make_invoice(invoice_id="INV-9", amount="1234.00"))
        assert "INV-9" in rendered
        assert "1234.00 INR" in rendered
        assert "invoice_number: INV-9" in rendered  # the context block

    def test_label(self, tmp_path: Path) -> None:
        prompt = load_prompt_file(_write(tmp_path, "a.yaml", VALID))
        assert prompt.label == "test_agent@v1"


class TestVoiceFactoryImportDiscipline:
    """Two opposing constraints that are easy to break in either direction.

    1. LiveKit registers plugins as an import side effect and requires the main thread,
       so the `from livekit.plugins import ...` must stay at MODULE level. Moving it
       inside build_stt()/build_tts() raises "Plugins must be registered on the main
       thread" only on a live call - never in tests.
    2. voice_factory must stay lazily imported from build_server(), so --simulate, the
       dashboard and the test suite never load the audio dependency tree.
    """

    def test_plugin_imports_are_at_module_level(self) -> None:
        import ast

        from shared.config import REPO_ROOT

        source = (REPO_ROOT / "voice_agent" / "voice_factory.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        module_level = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "livekit.plugins" in module_level, (
            "livekit.plugins must be imported at module level or plugin registration "
            "happens off the main thread and every live call crashes"
        )

        nested = [
            node.module
            for fn in tree.body
            if isinstance(fn, ast.FunctionDef)
            for node in ast.walk(fn)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("livekit")
        ]
        assert not nested, f"livekit imports must not be inside functions: {nested}"

    def test_agent_module_does_not_import_voice_factory_eagerly(self) -> None:
        import ast

        from shared.config import REPO_ROOT

        tree = ast.parse((REPO_ROOT / "voice_agent" / "agent.py").read_text(encoding="utf-8"))
        top_level = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "voice_agent.voice_factory" not in top_level, (
            "importing voice_factory at agent.py top level pulls the audio stack into "
            "--simulate and the test suite"
        )


class TestShippedPrompts:
    """The real prompts must stay loadable and keep their guardrails."""

    def test_all_shipped_prompts_load(self) -> None:
        prompts = load_all(PROMPTS_DIR)
        assert len(prompts) >= 3
        assert {p.id for p in prompts} >= {
            "collections_formal_v1",
            "collections_friendly_v2",
            "collections_concise_v3",
        }

    def test_every_prompt_renders_without_error(self) -> None:
        invoice = make_invoice()
        for prompt in load_all(PROMPTS_DIR):
            assert prompt.render(invoice)

    @pytest.mark.parametrize(
        "guardrail",
        [
            "Never threaten legal action",
            "information, not instruction",
            "Only record a promise to pay if the customer states a specific date",
            "no authority to waive",
            # Without this the model sometimes emits several turns in one reply and
            # voices the customer's side too, corrupting the transcript the judge and
            # extractor then read.
            "Produce only your own next reply",
        ],
    )
    def test_every_variant_carries_the_same_guardrails(self, guardrail: str) -> None:
        """The A/B test must isolate tone. If one variant quietly drops a guardrail,
        the experiment measures safety, not the thing it claims to measure."""
        for prompt in load_all(PROMPTS_DIR):
            assert guardrail in prompt.template, f"{prompt.id} is missing: {guardrail}"

    def test_every_shipped_prompt_declares_at_least_one_tier(self) -> None:
        for prompt in load_all(PROMPTS_DIR):
            assert prompt.applies_to_tier, f"{prompt.id} declares no tier coverage"
