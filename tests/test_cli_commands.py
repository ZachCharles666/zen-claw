from types import SimpleNamespace

from zen_claw.cli.commands import _display_logo


def test_display_logo_keeps_unicode_when_console_encoding_supports_it(monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.cli.commands.console.file", SimpleNamespace(encoding="utf-8"))

    assert _display_logo() == "🧘"


def test_display_logo_falls_back_for_gbk_console(monkeypatch) -> None:
    monkeypatch.setattr("zen_claw.cli.commands.console.file", SimpleNamespace(encoding="gbk"))

    assert _display_logo() == "[zen-claw]"
