import shutil
import subprocess
from types import SimpleNamespace

import pytest
import typer

from zen_claw.cli.commands import _display_logo, _get_bridge_dir


def test_display_logo_keeps_unicode_when_console_encoding_supports_it(monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.cli.commands.console.file", SimpleNamespace(encoding="utf-8"))

    assert _display_logo() == "🧘"


def test_display_logo_falls_back_for_gbk_console(monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.cli.commands.console.file", SimpleNamespace(encoding="gbk"))

    assert _display_logo() == "[zen-claw]"


def test_get_bridge_dir_refuses_unlocked_install(monkeypatch, tmp_path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr("zen_claw.cli.commands.Path.home", lambda: home_dir)
    monkeypatch.setattr(shutil, "which", lambda name: "npm")

    def fake_copytree(_src, dst, ignore=None):  # noqa: ARG001
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "package.json").write_text('{"name":"bridge"}', encoding="utf-8")
        return dst

    monkeypatch.setattr(shutil, "copytree", fake_copytree)

    with pytest.raises(typer.Exit) as exc_info:
        _get_bridge_dir()

    assert exc_info.value.exit_code == 1


def test_get_bridge_dir_uses_npm_ci_when_lockfile_exists(monkeypatch, tmp_path) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr("zen_claw.cli.commands.Path.home", lambda: home_dir)
    monkeypatch.setattr(shutil, "which", lambda name: "npm")
    commands_seen: list[list[str]] = []

    def fake_copytree(_src, dst, ignore=None):  # noqa: ARG001
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "package.json").write_text('{"name":"bridge"}', encoding="utf-8")
        (dst / "package-lock.json").write_text("{}", encoding="utf-8")
        return dst

    def fake_run(cmd, cwd=None, check=None, capture_output=None):  # noqa: ARG001
        commands_seen.append(list(cmd))
        return SimpleNamespace(stderr=b"")

    monkeypatch.setattr(shutil, "copytree", fake_copytree)
    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge_dir = _get_bridge_dir()

    assert bridge_dir == home_dir / ".zen-claw" / "bridge"
    assert commands_seen[0] == ["npm", "ci"]
    assert commands_seen[1] == ["npm", "run", "build"]
