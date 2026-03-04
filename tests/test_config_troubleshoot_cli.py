import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_troubleshoot_passes_for_consistent_openrouter(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {
                    "openrouter": {
                        "apiKey": "sk-or-test",
                        "apiBase": "https://openrouter.ai/api/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    out = CliRunner().invoke(app, ["config", "troubleshoot", "--config", str(cfg)])
    assert out.exit_code == 0
    assert "PASS" in out.output


def test_config_troubleshoot_warns_on_unrecognized_model_prefix(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "claude-opus-raw-name"}},
                "providers": {"anthropic": {"apiKey": "sk-anthropic-test"}},
            }
        ),
        encoding="utf-8",
    )
    out = CliRunner().invoke(app, ["config", "troubleshoot", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    assert "模型名前缀未识别" in out.output


def test_config_troubleshoot_warns_on_provider_model_mismatch(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {
                    "anthropic": {"apiKey": "sk-anthropic-test"},
                    "openrouter": {"apiKey": ""},
                },
            }
        ),
        encoding="utf-8",
    )
    out = CliRunner().invoke(app, ["config", "troubleshoot", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    assert "selected->anthropic" in out.output
