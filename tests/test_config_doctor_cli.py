import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app


def test_config_doctor_fails_without_any_provider_key(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {"openrouter": {"apiKey": ""}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 1
    assert "No provider API key configured" in out.output


def test_config_doctor_passes_with_openrouter_consistent_config(tmp_path: Path) -> None:
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

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 0
    assert "PASS" in out.output


def test_config_doctor_strict_fails_on_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {"openrouter": {"apiKey": "sk-or-test"}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    assert "built-in gateway default" in out.output


def test_config_doctor_prints_troubleshooting_and_production_suggestions(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {"openrouter": {"apiKey": "sk-or-test"}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 0
    assert "Troubleshooting" in out.output
    assert "Config Self-check Commands" in out.output
    assert "Production Suggestions" in out.output
    assert "zen-claw config wizard --dry-run" in out.output
    assert "config_path=" in out.output


def test_config_doctor_strict_fails_when_model_prefix_is_unknown_and_provider_falls_back(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "stepfun/step-3.5-flash:free"}},
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

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "falls back to `openrouter`" in lowered
    assert "provider/model" in lowered


def test_config_doctor_fails_on_duplicated_gateway_key(tmp_path: Path) -> None:
    duplicated = "sk-or-v1-demo-key" * 2
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"}},
                "providers": {
                    "openrouter": {
                        "apiKey": duplicated,
                        "apiBase": "https://openrouter.ai/api/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "secret twice" in lowered
    assert "paste the api key once" in lowered
    assert "该 key 看起来像把同一个 secret 粘贴了两次" in out.output


def test_config_doctor_warns_when_browser_sidecar_allowlist_missing(tmp_path: Path) -> None:
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
                "tools": {"network": {"browser": {"mode": "sidecar"}}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(
        app,
        ["config", "doctor", "--config", str(cfg), "--strict"],
        env={
            "NET_PROXY_ALLOW_DOMAINS": "",
            "BROWSER_SIDECAR_ALLOW_DOMAINS": "",
        },
    )
    assert out.exit_code == 1
    assert "allowlist" in out.output.lower()


def test_config_doctor_no_browser_allowlist_warning_when_env_set(tmp_path: Path) -> None:
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
                "tools": {"network": {"browser": {"mode": "sidecar"}}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(
        app,
        ["config", "doctor", "--config", str(cfg), "--strict"],
        env={
            "NET_PROXY_ALLOW_DOMAINS": "github.com",
            "BROWSER_SIDECAR_ALLOW_DOMAINS": "",
        },
    )
    assert out.exit_code == 0
    assert "allowlist" not in out.output.lower()


def test_config_doctor_fails_when_webhook_trigger_enabled_without_auth_path(tmp_path: Path) -> None:
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
                "channels": {
                    "webhookTrigger": {
                        "enabled": True,
                        "secret": "",
                        "apiKey": "",
                        "ipAllowlist": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "webhook_trigger" in lowered
    assert "auth path" in lowered


def test_config_doctor_fails_when_matrix_enabled_without_any_auth_path(tmp_path: Path) -> None:
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
                "channels": {
                    "matrix": {
                        "enabled": True,
                        "homeserver": "https://matrix.org",
                        "accessToken": "",
                        "username": "",
                        "password": "",
                        "autoLogin": False,
                        "autoRegister": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg)])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "matrix" in lowered
    assert "auth path" in lowered


def test_config_doctor_warns_empty_agent_profile_in_strict_mode(tmp_path: Path) -> None:
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
                "channels": {"webchat": {"agentProfile": ""}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "channels.webchat.agent" in lowered
    assert "empty" in lowered


def test_config_doctor_warns_unknown_agent_profile_reference(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"},
                    "profiles": {"finance_writer": {"displayName": "Finance Writer"}},
                },
                "providers": {
                    "openrouter": {
                        "apiKey": "sk-or-test",
                        "apiBase": "https://openrouter.ai/api/v1",
                    }
                },
                "channels": {"webchat": {"agentProfile": "missing_writer"}},
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "unknown profile" in lowered
    assert "missing_writer" in lowered


def test_config_doctor_warns_profile_prompt_file_outside_workspace(tmp_path: Path) -> None:
    outside_prompt = tmp_path / "outside.md"
    outside_prompt.write_text("prompt", encoding="utf-8")
    workspace = tmp_path / "finance-ws"
    workspace.mkdir(parents=True, exist_ok=True)

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"},
                    "profiles": {
                        "finance_writer": {
                            "workspace": str(workspace),
                            "systemPromptFile": str(outside_prompt),
                        }
                    },
                },
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

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "points outside its workspace" in lowered
    assert "finance_writer" in lowered


def test_config_doctor_warns_profile_tool_binding_overlap(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": "openrouter/anthropic/claude-3.5-sonnet"},
                    "profiles": {
                        "finance_writer": {
                            "allowedTools": ["read_file", "exec"],
                            "deniedTools": ["exec"],
                        }
                    },
                },
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

    out = CliRunner().invoke(app, ["config", "doctor", "--config", str(cfg), "--strict"])
    assert out.exit_code == 1
    lowered = out.output.lower()
    assert "allowedtools/deniedtools" in lowered
    assert "overlaps" in lowered
    assert "exec" in lowered
