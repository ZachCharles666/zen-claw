"""Tests for semver-based skill version resolution."""

import json
import zipfile
from pathlib import Path

import pytest

from zen_claw.agent.skills import SkillsLoader


def _make_loader(workspace: Path) -> SkillsLoader:
    loader = SkillsLoader(workspace)
    return loader


def _make_skill_dir(skills_dir: Path, physical_name: str, version: str) -> Path:
    """Create a minimal skill directory with given version in manifest."""
    skill_dir = skills_dir / physical_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {physical_name}\nA test skill.", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "myskill",
                "version": version,
                "description": "test skill",
                "permissions": [],
                "trust": "untrusted",
            }
        ),
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)
    monkeypatch.setattr(SkillsLoader, "_journal_recovery", lambda self: None)


def test_highest_semver_selected(tmp_path):
    """resolve_physical_path picks the highest semver among candidates."""
    skills_dir = tmp_path / "skills"
    _make_skill_dir(skills_dir, "myskill_v100", "1.0.0")
    _make_skill_dir(skills_dir, "myskill_v200", "2.0.0")
    _make_skill_dir(skills_dir, "myskill_v110", "1.1.0")

    loader = _make_loader(tmp_path)
    loader._skill_mapping = {
        "myskill": ["myskill_v100", "myskill_v200", "myskill_v110"]
    }

    result = loader.resolve_physical_path("myskill")
    assert result is not None
    assert result.name == "myskill_v200"


def test_version_pin_exact_match(tmp_path):
    """resolve_physical_path respects version_pin for exact match."""
    skills_dir = tmp_path / "skills"
    _make_skill_dir(skills_dir, "myskill_v100", "1.0.0")
    _make_skill_dir(skills_dir, "myskill_v110", "1.1.0")
    _make_skill_dir(skills_dir, "myskill_v200", "2.0.0")

    loader = _make_loader(tmp_path)
    loader._skill_mapping = {
        "myskill": ["myskill_v100", "myskill_v110", "myskill_v200"]
    }

    result = loader.resolve_physical_path("myskill", version_pin="1.1.0")
    assert result is not None
    assert result.name == "myskill_v110"


def test_version_pin_no_match_returns_none(tmp_path):
    """resolve_physical_path returns None when pinned version doesn't exist."""
    skills_dir = tmp_path / "skills"
    _make_skill_dir(skills_dir, "myskill_v100", "1.0.0")

    loader = _make_loader(tmp_path)
    loader._skill_mapping = {"myskill": ["myskill_v100"]}

    result = loader.resolve_physical_path("myskill", version_pin="9.9.9")
    assert result is None


def test_invalid_version_treated_as_zero(tmp_path):
    """Skill with unparseable version string is treated as 0.0.0, not selected over valid ones."""
    skills_dir = tmp_path / "skills"
    _make_skill_dir(skills_dir, "myskill_good", "1.0.0")

    bad_dir = skills_dir / "myskill_bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "SKILL.md").write_text("# bad", encoding="utf-8")
    (bad_dir / "manifest.json").write_text(
        json.dumps({"name": "myskill", "version": "not-a-version", "description": "x",
                    "permissions": [], "trust": "untrusted"}),
        encoding="utf-8",
    )

    loader = _make_loader(tmp_path)
    loader._skill_mapping = {"myskill": ["myskill_bad", "myskill_good"]}

    result = loader.resolve_physical_path("myskill")
    assert result is not None
    assert result.name == "myskill_good"


def test_single_candidate_returned(tmp_path):
    """Single candidate is returned regardless."""
    skills_dir = tmp_path / "skills"
    _make_skill_dir(skills_dir, "myskill_v100", "1.0.0")

    loader = _make_loader(tmp_path)
    loader._skill_mapping = {"myskill": ["myskill_v100"]}

    result = loader.resolve_physical_path("myskill")
    assert result is not None
    assert result.name == "myskill_v100"


def test_zip_single_file_size_limit(tmp_path):
    """_safe_extract_zip rejects archives containing a single oversized file."""
    zip_path = tmp_path / "big.zip"
    big_content = b"x" * (5 * 1024 * 1024 + 1)  # 5MB + 1 byte

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("big_file.txt", big_content)

    loader = _make_loader(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with zipfile.ZipFile(zip_path, "r") as zf:
        ok, err = loader._safe_extract_zip(zf, out_dir)

    assert not ok
    assert "too large" in err


def test_zip_cumulative_size_limit(tmp_path):
    """_safe_extract_zip rejects archives whose total uncompressed size exceeds 10MB."""
    zip_path = tmp_path / "bomb.zip"
    chunk = b"y" * (1024 * 1024)  # 1MB per file, 11 files = 11MB total

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(11):
            zf.writestr(f"file_{i}.txt", chunk)

    loader = _make_loader(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with zipfile.ZipFile(zip_path, "r") as zf:
        ok, err = loader._safe_extract_zip(zf, out_dir)

    assert not ok
    assert "too large" in err
