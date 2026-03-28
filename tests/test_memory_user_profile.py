"""Tests for MemoryStore.get_user_profile / update_user_profile and context injection."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from zen_claw.agent.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=tmp_path)


# ── get_user_profile ───────────────────────────────────────────────────────────


def test_get_user_profile_returns_empty_when_missing(store: MemoryStore):
    assert store.get_user_profile() == ""


def test_get_user_profile_returns_content(store: MemoryStore):
    store.user_profile_file.write_text("# User Profile\n\n- timezone: Asia/Shanghai\n", encoding="utf-8")
    content = store.get_user_profile()
    assert "timezone" in content
    assert "Asia/Shanghai" in content


# ── update_user_profile ────────────────────────────────────────────────────────


def test_update_user_profile_creates_file(store: MemoryStore):
    assert not store.user_profile_file.exists()
    store.update_user_profile("timezone", "Asia/Shanghai")
    assert store.user_profile_file.exists()


def test_update_user_profile_writes_key_value(store: MemoryStore):
    store.update_user_profile("timezone", "Asia/Shanghai")
    content = store.get_user_profile()
    assert "- timezone: Asia/Shanghai" in content


def test_update_user_profile_upserts_existing_key(store: MemoryStore):
    store.update_user_profile("timezone", "UTC")
    store.update_user_profile("timezone", "Asia/Shanghai")
    content = store.get_user_profile()
    assert content.count("- timezone:") == 1
    assert "Asia/Shanghai" in content
    assert "UTC" not in content


def test_update_user_profile_multiple_keys(store: MemoryStore):
    store.update_user_profile("timezone", "Asia/Shanghai")
    store.update_user_profile("preferred_calendar", "google")
    store.update_user_profile("briefing_time", "08:30")
    content = store.get_user_profile()
    assert "timezone: Asia/Shanghai" in content
    assert "preferred_calendar: google" in content
    assert "briefing_time: 08:30" in content


def test_update_user_profile_sanitizes_key(store: MemoryStore):
    store.update_user_profile("My Pref!!", "value")
    content = store.get_user_profile()
    assert "my_pref__" in content


def test_update_user_profile_ignores_empty_key(store: MemoryStore):
    store.update_user_profile("", "some value")
    assert not store.user_profile_file.exists()


def test_update_user_profile_strips_newlines_from_value(store: MemoryStore):
    store.update_user_profile("note", "line one\nline two")
    content = store.get_user_profile()
    assert "\n" not in content.split("- note:")[1].split("\n")[0]


def test_update_user_profile_stays_inside_memory_dir(store: MemoryStore):
    """Writing user profile stays within memory dir, no path traversal."""
    assert store.user_profile_file.parent.resolve() == store.memory_dir.resolve()


# ── Symlink protection ─────────────────────────────────────────────────────────


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="Creating symlinks requires elevated privileges on Windows",
)
def test_update_user_profile_refuses_symlink(store: MemoryStore, tmp_path: Path):
    target = tmp_path / "external_file.txt"
    target.write_text("external", encoding="utf-8")
    store.user_profile_file.symlink_to(target)
    with pytest.raises(PermissionError, match="symlink"):
        store.update_user_profile("key", "value")


# ── Context injection ──────────────────────────────────────────────────────────


def test_build_system_prompt_includes_user_profile(tmp_path: Path):
    """USER_PROFILE.md content appears in system prompt."""
    from unittest.mock import MagicMock, patch

    ws = tmp_path
    mem = MemoryStore(workspace=ws)
    mem.update_user_profile("timezone", "Asia/Tokyo")
    mem.update_user_profile("preferred_calendar", "outlook")

    with patch("zen_claw.agent.context.MemoryStore", return_value=mem):
        from zen_claw.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=ws)
        cb.memory = mem  # inject directly

        prompt = cb.build_system_prompt()

    assert "User Profile" in prompt
    assert "timezone: Asia/Tokyo" in prompt
    assert "preferred_calendar: outlook" in prompt


def test_build_system_prompt_no_user_profile_section_when_empty(tmp_path: Path):
    """No User Profile section when USER_PROFILE.md is absent."""
    from unittest.mock import patch

    ws = tmp_path
    mem = MemoryStore(workspace=ws)

    with patch("zen_claw.agent.context.MemoryStore", return_value=mem):
        from zen_claw.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=ws)
        cb.memory = mem

        prompt = cb.build_system_prompt()

    assert "User Profile" not in prompt
