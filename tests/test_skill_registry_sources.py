"""Tests for multi-source skill registry: GitHubSkillSource, LocalDirSkillSource."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zen_claw.skills.sources import GitHubSkillSource, LocalDirSkillSource, SkillSourceAdapter


# ── protocol compliance ───────────────────────────────────────────────────────


def test_github_source_implements_adapter_protocol() -> None:
    src = GitHubSkillSource("github:owner/repo", source_name="test-github")
    assert isinstance(src, SkillSourceAdapter)


def test_local_source_implements_adapter_protocol(tmp_path: Path) -> None:
    src = LocalDirSkillSource(str(tmp_path), source_name="test-local")
    assert isinstance(src, SkillSourceAdapter)


# ── GitHubSkillSource: URL parsing ───────────────────────────────────────────


def test_github_source_parses_shorthand_url() -> None:
    src = GitHubSkillSource("github:myorg/my-skills")
    assert src._owner == "myorg"
    assert src._repo == "my-skills"
    assert src._path == ""


def test_github_source_parses_shorthand_with_subpath() -> None:
    src = GitHubSkillSource("github:myorg/my-skills/skills-dir")
    assert src._owner == "myorg"
    assert src._repo == "my-skills"
    assert src._path == "skills-dir"


def test_github_source_parses_full_https_url() -> None:
    src = GitHubSkillSource("https://github.com/myorg/my-skills")
    assert src._owner == "myorg"
    assert src._repo == "my-skills"
    assert src._path == ""


def test_github_source_parses_tree_url_strips_branch() -> None:
    src = GitHubSkillSource("https://github.com/myorg/my-skills/tree/main/skills")
    assert src._owner == "myorg"
    assert src._repo == "my-skills"
    assert src._path == "skills"


def test_github_source_name_is_set() -> None:
    src = GitHubSkillSource("github:o/r", source_name="my-source")
    assert src.source_name == "my-source"


# ── GitHubSkillSource: list_entries ──────────────────────────────────────────


def _mock_api_get_side_effect(url: str):
    """Return realistic GitHub API responses."""
    if url.endswith("/contents") or url.endswith("/contents/"):
        return [
            {"type": "dir", "name": "my_skill", "path": "my_skill"},
            {"type": "file", "name": "README.md", "path": "README.md"},
        ]
    if url.endswith("/contents/my_skill"):
        return [
            {"type": "file", "name": "SKILL.md", "download_url": "https://raw.example.com/SKILL.md"},
            {"type": "file", "name": "manifest.json", "download_url": "https://raw.example.com/manifest.json"},
        ]
    return []


def test_github_list_entries_returns_skill_from_valid_dir() -> None:
    manifest = {
        "name": "my_skill",
        "version": "1.0.0",
        "description": "Test skill",
        "author": "test-author",
        "permissions": ["read_file"],
        "tags": ["utility"],
    }

    src = GitHubSkillSource("github:owner/repo")
    with (
        patch.object(src, "_api_get", side_effect=_mock_api_get_side_effect),
        patch.object(src, "_raw_get", return_value=json.dumps(manifest).encode()),
    ):
        entries = src.list_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "my_skill"
    assert entry.version == "1.0.0"
    assert entry.author == "test-author"
    assert "read_file" in entry.permissions


def test_github_list_entries_skips_dir_without_skill_md() -> None:
    def _api_get(url: str):
        if "contents" in url and "no_manifest" not in url:
            return [{"type": "dir", "name": "no_manifest", "path": "no_manifest"}]
        return [{"type": "file", "name": "README.md"}]  # no SKILL.md

    src = GitHubSkillSource("github:owner/repo")
    with patch.object(src, "_api_get", side_effect=_api_get):
        entries = src.list_entries()

    assert entries == []


def test_github_list_entries_returns_empty_on_network_error() -> None:
    src = GitHubSkillSource("github:owner/repo")
    with patch.object(src, "_api_get", side_effect=Exception("connection refused")):
        entries = src.list_entries()

    assert entries == []


def test_github_list_entries_returns_empty_when_api_returns_non_list() -> None:
    src = GitHubSkillSource("github:owner/repo")
    with patch.object(src, "_api_get", return_value={"message": "Not Found"}):
        entries = src.list_entries()

    assert entries == []


# ── GitHubSkillSource: fetch_skill_zip ───────────────────────────────────────


def test_github_fetch_skill_zip_returns_valid_zip() -> None:
    def _api_get(url: str):
        return [
            {"type": "file", "name": "SKILL.md", "download_url": "https://raw/SKILL.md"},
            {"type": "file", "name": "manifest.json", "download_url": "https://raw/manifest.json"},
        ]

    src = GitHubSkillSource("github:owner/repo")
    with (
        patch.object(src, "_api_get", side_effect=_api_get),
        patch.object(src, "_raw_get", return_value=b"content"),
    ):
        zipped = src.fetch_skill_zip("my_skill")

    assert zipped is not None
    with zipfile.ZipFile(BytesIO(zipped)) as zf:
        names = zf.namelist()
    assert "my_skill/SKILL.md" in names
    assert "my_skill/manifest.json" in names


def test_github_fetch_skill_zip_returns_none_when_no_skill_md() -> None:
    src = GitHubSkillSource("github:owner/repo")
    with patch.object(src, "_api_get", return_value=[
        {"type": "file", "name": "only_readme.md", "download_url": "https://raw/readme.md"},
    ]):
        result = src.fetch_skill_zip("no_skill_md")

    assert result is None


def test_github_fetch_skill_zip_returns_none_on_api_error() -> None:
    src = GitHubSkillSource("github:owner/repo")
    with patch.object(src, "_api_get", side_effect=Exception("404")):
        result = src.fetch_skill_zip("missing_skill")

    assert result is None


# ── LocalDirSkillSource ───────────────────────────────────────────────────────


def test_local_source_name_is_set(tmp_path: Path) -> None:
    src = LocalDirSkillSource(str(tmp_path), source_name="local-dev")
    assert src.source_name == "local-dev"


def test_local_source_returns_empty_for_nonexistent_dir() -> None:
    src = LocalDirSkillSource("/nonexistent/path/xyz123")
    assert src.list_entries() == []


def test_local_source_discovers_valid_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my_local_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My skill", encoding="utf-8")
    manifest = {
        "name": "my_local_skill",
        "version": "0.2.0",
        "description": "A local skill",
        "author": "dev",
        "permissions": ["web_fetch"],
        "tags": ["network"],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    src = LocalDirSkillSource(str(tmp_path))
    entries = src.list_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "my_local_skill"
    assert entry.version == "0.2.0"
    assert "web_fetch" in entry.permissions


def test_local_source_uses_dir_name_when_manifest_missing(tmp_path: Path) -> None:
    skill_dir = tmp_path / "nameless_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# nameless", encoding="utf-8")
    # No manifest.json

    src = LocalDirSkillSource(str(tmp_path))
    entries = src.list_entries()

    assert len(entries) == 1
    assert entries[0].name == "nameless_skill"


def test_local_source_skips_files_at_root(tmp_path: Path) -> None:
    (tmp_path / "not_a_skill.md").write_text("hello")
    src = LocalDirSkillSource(str(tmp_path))
    assert src.list_entries() == []


def test_local_source_skips_dir_without_skill_md(tmp_path: Path) -> None:
    no_skill = tmp_path / "no_skill_md"
    no_skill.mkdir()
    (no_skill / "manifest.json").write_text("{}", encoding="utf-8")

    src = LocalDirSkillSource(str(tmp_path))
    assert src.list_entries() == []


def test_local_source_handles_malformed_manifest_gracefully(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad_manifest"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# skill", encoding="utf-8")
    (skill_dir / "manifest.json").write_text("not json !!!", encoding="utf-8")

    src = LocalDirSkillSource(str(tmp_path))
    entries = src.list_entries()

    # Should still discover the skill using the dir name as fallback
    assert len(entries) == 1
    assert entries[0].name == "bad_manifest"


def test_local_source_discovers_multiple_skills(tmp_path: Path) -> None:
    for skill_name in ("skill_a", "skill_b", "skill_c"):
        d = tmp_path / skill_name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {skill_name}")
        (d / "manifest.json").write_text(json.dumps({"name": skill_name, "version": "1.0.0"}))

    src = LocalDirSkillSource(str(tmp_path))
    entries = src.list_entries()

    assert len(entries) == 3
    names = {e.name for e in entries}
    assert names == {"skill_a", "skill_b", "skill_c"}
