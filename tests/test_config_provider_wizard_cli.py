import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_providers_json_contains_global_and_china_rows() -> None:
    out = CliRunner().invoke(app, ["config", "providers", "--json"])
    assert out.exit_code == 0
    rows = json.loads(out.output)
    ids = {row["id"] for row in rows}
    coverages = {row["coverage"] for row in rows}
    assert "openrouter" in ids
    assert "zhipu" in ids
    assert "global" in coverages
    assert "china" in coverages


def test_config_wizard_write_creates_or_updates_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    out = CliRunner().invoke(
        app,
        [
            "config",
            "wizard",
            "--config",
            str(cfg),
            "--provider",
            "openrouter",
            "--api-key",
            "sk-or-test",
            "--model",
            "openrouter/anthropic/claude-3.5-sonnet",
            "--api-base",
            "https://openrouter.ai/api/v1",
            "--yes",
        ],
    )
    assert out.exit_code == 0
    assert "Config Wizard Summary" in out.output
    assert cfg.exists()

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["providers"]["openrouter"]["apiKey"] == "sk-or-test"
    assert payload["providers"]["openrouter"]["apiBase"] == "https://openrouter.ai/api/v1"
    assert payload["agents"]["defaults"]["model"] == "openrouter/anthropic/claude-3.5-sonnet"


def test_config_wizard_dry_run_keeps_file_unchanged(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "anthropic/claude-opus-4-5"}},
                "providers": {"openrouter": {"apiKey": ""}},
            }
        ),
        encoding="utf-8",
    )
    before = cfg.read_text(encoding="utf-8")
    out = CliRunner().invoke(
        app,
        [
            "config",
            "wizard",
            "--config",
            str(cfg),
            "--provider",
            "openrouter",
            "--api-key",
            "sk-or-new",
            "--model",
            "openrouter/deepseek/deepseek-chat",
            "--api-base",
            "https://openrouter.ai/api/v1",
            "--dry-run",
        ],
    )
    assert out.exit_code == 0
    assert "Dry-run" in out.output
    assert cfg.read_text(encoding="utf-8") == before
