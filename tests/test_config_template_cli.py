import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_template_default_profile_stdout() -> None:
    out = CliRunner().invoke(app, ["config", "template"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert data["meta"]["profile"] == "global-openrouter"
    assert data["providers"]["openrouter"]["apiKey"] == "<API_KEY>"
    assert data["agents"]["defaults"]["model"].startswith("openrouter/")


def test_config_template_write_file_and_show_wizard(tmp_path: Path) -> None:
    out_file = tmp_path / "tpl" / "china-deepseek.json"
    out = CliRunner().invoke(
        app,
        [
            "config",
            "template",
            "--profile",
            "china-deepseek",
            "--out",
            str(out_file),
            "--show-wizard",
        ],
    )
    assert out.exit_code == 0
    assert out_file.exists()
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["meta"]["profile"] == "china-deepseek"
    assert payload["providers"]["deepseek"]["apiKey"] == "<API_KEY>"
    assert "Equivalent wizard command:" in out.output


def test_config_template_rejects_unknown_profile() -> None:
    out = CliRunner().invoke(app, ["config", "template", "--profile", "unknown-x"])
    assert out.exit_code == 1
    assert "Unsupported profile" in out.output
