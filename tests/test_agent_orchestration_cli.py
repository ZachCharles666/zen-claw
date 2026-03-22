import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import app
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_agent_list_prints_registered_profiles(monkeypatch, tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                    "profiles": {
                        "finance_writer": {
                            "displayName": "Finance Writer",
                            "workspace": str(tmp_path / "finance-ws"),
                            "model": "deepseek-chat",
                            "skillNames": ["content_gen"],
                        }
                    },
                }
            }
        )
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)

    out = CliRunner().invoke(app, ["agent", "list", "--json"])
    assert out.exit_code == 0
    rows = json.loads(out.output)
    assert [row["agent_id"] for row in rows] == ["default", "finance_writer"]
    assert rows[1]["model"] == "deepseek-chat"
    assert rows[1]["skill_names"] == ["content_gen"]

    alias = CliRunner().invoke(app, ["agent-list", "--json"])
    assert alias.exit_code == 0
    assert json.loads(alias.output) == rows


def test_agent_cli_runs_against_selected_profile(monkeypatch, tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "defaults": {
                        "workspace": str(tmp_path / "default-ws"),
                        "model": "default-model",
                        "enablePlanning": True,
                    },
                    "profiles": {
                        "finance_writer": {
                            "displayName": "Finance Writer",
                            "workspace": str(tmp_path / "finance-ws"),
                            "model": "deepseek-chat",
                            "enablePlanning": False,
                            "skillNames": ["content_gen"],
                        }
                    },
                }
            }
        )
    )

    async def _fake_process_direct(self, message, session_key, **kwargs):
        _ = kwargs
        return f"{self.workspace}|{self.model}|planning={self.enable_planning}|{message}|{session_key}"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.cli.commands._print_effective_tool_backends", lambda _cfg: None)
    monkeypatch.setattr(
        "zen_claw.cli.commands._make_provider", lambda _cfg, model=None: object()
    )
    monkeypatch.setattr("zen_claw.agent.loop.AgentLoop.process_direct", _fake_process_direct)

    out = CliRunner().invoke(
        app,
        ["agent", "--agent-profile", "finance_writer", "-m", "hello", "--session", "cli:test"],
    )
    assert out.exit_code == 0
    assert "finance_writer (registered)" in out.output
    assert "Skill Slot: content_gen" in out.output
    assert "finance-ws|deepseek-chat|planning=False|hello|" in out.output


def test_agent_chat_subcommand_runs_against_selected_profile(monkeypatch, tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                    "profiles": {
                        "finance_writer": {
                            "workspace": str(tmp_path / "finance-ws"),
                            "model": "deepseek-chat",
                        }
                    },
                }
            }
        )
    )

    async def _fake_process_direct(self, message, session_key, **kwargs):
        _ = kwargs
        return f"{self.workspace}|{self.model}|{message}|{session_key}"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.cli.commands._print_effective_tool_backends", lambda _cfg: None)
    monkeypatch.setattr("zen_claw.cli.commands._make_provider", lambda _cfg, model=None: object())
    monkeypatch.setattr("zen_claw.agent.loop.AgentLoop.process_direct", _fake_process_direct)

    out = CliRunner().invoke(
        app,
        ["agent", "chat", "finance_writer", "-m", "hello", "--session", "cli:test"],
    )
    assert out.exit_code == 0
    assert "finance_writer (registered)" in out.output
    assert "finance-ws|deepseek-chat|hello|cli:test" in out.output


def test_agent_test_subcommand_uses_default_smoke_prompt(monkeypatch, tmp_path: Path) -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                    "profiles": {"finance_writer": {"workspace": str(tmp_path / "finance-ws")}},
                }
            }
        )
    )
    seen: list[str] = []

    async def _fake_process_direct(self, message, session_key, **kwargs):
        _ = kwargs, session_key
        seen.append(message)
        return "ok"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.cli.commands._print_effective_tool_backends", lambda _cfg: None)
    monkeypatch.setattr("zen_claw.cli.commands._make_provider", lambda _cfg, model=None: object())
    monkeypatch.setattr("zen_claw.agent.loop.AgentLoop.process_direct", _fake_process_direct)

    out = CliRunner().invoke(app, ["agent", "test", "finance_writer"])
    assert out.exit_code == 0
    assert seen == ["ping"]
