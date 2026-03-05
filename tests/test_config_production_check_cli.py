import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_production_check_passes_with_gateway_and_env_policy(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "config.prod.json"
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
    monkeypatch.setenv("zen_claw_KEY_ROTATION_DAYS", "60")
    monkeypatch.setenv("zen_claw_ENV", "prod")

    out = CliRunner().invoke(app, ["config", "production-check", "--config", str(cfg), "--strict"])
    assert out.exit_code == 0
    assert "Config Production Check" in out.output
    assert "Unified gateway egress" in out.output
    assert "Key rotation policy set: 60 days" in out.output


def test_config_production_check_strict_fails_on_missing_policy_envs(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "anthropic/claude-opus-4-5"}},
                "providers": {"anthropic": {"apiKey": "sk-anthropic-test"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("zen_claw_KEY_ROTATION_DAYS", raising=False)
    monkeypatch.delenv("zen_claw_ENV", raising=False)

    out = CliRunner().invoke(app, ["config", "production-check", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    assert "Key rotation policy env" in out.output
    assert "Runtime env tag" in out.output


def test_config_production_check_fails_on_invalid_rotation_value(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "config.prod.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {"openrouter": {"apiKey": "sk-or-test"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("zen_claw_KEY_ROTATION_DAYS", "zero")
    monkeypatch.setenv("zen_claw_ENV", "prod")

    out = CliRunner().invoke(app, ["config", "production-check", "--config", str(cfg)])
    assert out.exit_code == 1
    assert "zen_claw_KEY_ROTATION_DAYS" in out.output
