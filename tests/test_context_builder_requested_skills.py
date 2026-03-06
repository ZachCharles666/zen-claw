import json
from pathlib import Path

import pytest

from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.skills import SkillsLoader


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    # Mock mapping and time to prevent potentially slow/hanging I/O or crypto in CI
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


def _write_skill(root: Path, name: str, content: str, always: bool = False) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {"zen-claw": {"always": True}} if always else {"zen-claw": {}}
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: test skill\n"
        f"metadata: '{json.dumps(meta)}'\n"
        "---\n\n" + content + "\n",
        encoding="utf-8",
    )


def test_build_system_prompt_includes_requested_skill_content(tmp_path: Path) -> None:
    _write_skill(tmp_path, "foo", "FOO SKILL BODY")
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")

    prompt = ctx.build_system_prompt(skill_names=["foo"], memory_query="x")
    assert "# Requested Skills" in prompt
    assert "### Skill: foo" in prompt
    assert "FOO SKILL BODY" in prompt


def test_build_system_prompt_skips_disabled_requested_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "foo", "FOO SKILL BODY")
    # Disable foo via state file used by SkillsLoader.
    state_path = tmp_path / ".zen-claw" / "skills_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["foo"]}), encoding="utf-8")
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")

    prompt = ctx.build_system_prompt(skill_names=["foo"], memory_query="x")
    assert "### Skill: foo" not in prompt
