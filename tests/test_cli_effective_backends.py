from io import StringIO
from pathlib import Path

from rich.console import Console

from zen_claw.cli import commands
from zen_claw.config.schema import Config, ToolPolicyLayerConfig


def test_print_effective_tool_backends_includes_policy_summary(monkeypatch) -> None:
    cfg = Config()
    cfg.tools.policy.kill_switch_enabled = True
    cfg.tools.policy.kill_switch_reason = "maintenance"
    cfg.tools.policy.cron_allowed_channels = ["discord", "telegram"]
    cfg.tools.policy.cron_allowed_actions_by_channel = {
        "discord": ["list"],
        "telegram": ["add", "list", "remove"],
    }
    cfg.tools.policy.cron_require_remove_confirmation = True
    cfg.tools.policy.channel_policies = {
        "discord": ToolPolicyLayerConfig(deny=["exec"]),
        "cli": ToolPolicyLayerConfig(allow=["*"]),
    }

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_effective_tool_backends(cfg)
    output = buf.getvalue()
    assert "globalKillSwitch: True, reason=maintenance" in output
    assert "cronAllowedChannels: discord, telegram" in output
    assert "cronAllowedActionsByChannel: discord=list, telegram=add/list/remove" in output
    assert "cronRequireRemoveConfirmation: True" in output
    assert "channelPolicyScopes: cli, discord" in output
    assert "browser: mode=off" in output


def test_print_channel_rbac_status_summary_and_verbose(monkeypatch) -> None:
    cfg = Config()
    cfg.channels.telegram.admins = ["a1", "a2"]
    cfg.channels.telegram.users = ["u1"]
    cfg.channels.discord.users = ["du1"]

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_channel_rbac_status(cfg, verbose=True)
    output = buf.getvalue()
    assert "Channel RBAC" in output
    assert "telegram: enabled=True, admins=2, users=1" in output
    assert "discord: enabled=True, admins=0, users=1" in output
    assert "whatsapp: enabled=False, admins=0, users=0" in output
    assert "admin_ids: a1, a2" in output
    assert "user_ids: u1" in output


def test_print_policy_audit_matrix_includes_policy_and_skill_scope(monkeypatch) -> None:
    cfg = Config()
    cfg.tools.policy.channel_policies = {"discord": ToolPolicyLayerConfig(deny=["exec"])}

    class _FakeLoader:
        def __init__(self, workspace):
            self.workspace = workspace

        def validate_all_skill_manifests(self, strict=False):
            return [{"name": "demo", "ok": True, "errors": []}]

        def get_skill_manifest(self, name):
            return (
                {
                    "name": name,
                    "scopes": ["network"],
                    "permissions": ["web_search"],
                },
                [],
            )

    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader", _FakeLoader)

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_policy_audit_matrix(cfg)
    output = buf.getvalue()
    assert "Policy Audit Matrix" in output
    assert "policy.channel.discord" in output
    assert "skills.demo" in output
    assert "scopes=network" in output


def test_print_sidecar_status(monkeypatch) -> None:
    cfg = Config()

    def _fake_collect(_config):
        return [
            {
                "name": "sec-execd",
                "status": "running",
                "managed": True,
                "pid": 1234,
                "uptime": "00:00:05",
                "health": True,
            }
        ]

    monkeypatch.setattr("zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", _fake_collect)

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_sidecar_status(cfg)
    output = buf.getvalue()
    assert "Sidecar Status" in output
    assert "sec-execd: status=running" in output
    assert "pid=1234" in output


def test_print_channel_rate_limit_status_with_runtime_stats(monkeypatch, tmp_path: Path) -> None:
    import json

    cfg = Config()
    cfg.channels.outbound_rate_limit_mode = "delay"
    cfg.channels.outbound_rate_limit_per_sec = 2.0
    cfg.channels.outbound_rate_limit_burst = 5
    from zen_claw.config.schema import ChannelRateLimitConfig

    cfg.channels.outbound_rate_limit_by_channel = {
        "discord": ChannelRateLimitConfig(per_sec=1.0, burst=2, mode="drop")
    }

    stats_dir = tmp_path / "data" / "channels"
    stats_dir.mkdir(parents=True, exist_ok=True)
    stats_file = stats_dir / "rate_limit_stats.json"
    stats_file.write_text(
        json.dumps(
            {
                "channels": {
                    "discord": {"delayed_count": 3, "dropped_count": 7, "last_delay_ms": 120},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path / "data")

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_channel_rate_limit_status(cfg)
    output = buf.getvalue()
    assert "Channel Rate Limit" in output
    assert "default: mode=delay" in output
    assert "override.discord: mode=drop" in output
    assert "runtime.discord: delayed=3, dropped=7, lastDelayMs=120" in output


def test_print_node_token_rotation_status(monkeypatch) -> None:
    class _FakeNodeService:
        def __init__(self, _path):
            self.path = _path

        def scan_token_rotation(self, within_sec=3600, rotate=False, ttl_sec=None):
            assert within_sec == 3600
            assert rotate is False
            return {
                "ok": True,
                "checked": 5,
                "candidates": [
                    {"node_id": "n1", "reason": "revoked"},
                    {"node_id": "n2", "reason": "expired"},
                    {"node_id": "n3", "reason": "expiring_soon"},
                ],
                "rotated": [],
            }

    monkeypatch.setattr("zen_claw.node.service.NodeService", _FakeNodeService)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: Path("C:/tmp/data"))

    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, color_system=None)
    monkeypatch.setattr(commands, "console", test_console)

    commands._print_node_token_rotation_status(within_sec=3600)
    output = buf.getvalue()
    assert "Node Token Rotation" in output
    assert "checked=5, candidates=3" in output
    assert "revoked=1" in output
    assert "expired=1" in output
    assert "expiringSoon=1" in output
