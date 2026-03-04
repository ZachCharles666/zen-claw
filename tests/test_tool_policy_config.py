from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_tools_policy_default_values() -> None:
    config = Config()
    assert config.tools.policy.default_deny_tools == ["exec", "spawn"]
    assert config.tools.policy.kill_switch_enabled is False
    assert config.tools.policy.kill_switch_reason == ""
    assert config.tools.policy.cron_allowed_channels == []
    assert config.tools.policy.channel_policies == {}
    assert config.tools.policy.agent.allow == ["*"]
    assert config.tools.policy.subagent.allow is not None
    assert "read_file" in config.tools.policy.subagent.allow


def test_tools_policy_accepts_camel_case_config_keys() -> None:
    raw = {
        "tools": {
            "policy": {
                "defaultDenyTools": ["exec"],
                "killSwitchEnabled": True,
                "killSwitchReason": "incident-response",
                "cronAllowedChannels": ["cli", "telegram"],
                "channelPolicies": {
                    "discord": {"deny": ["exec", "spawn"]},
                    "cli": {"allow": ["*"]},
                },
                "agent": {"allow": ["read_file"], "deny": ["web_fetch"]},
                "subagent": {"allow": ["read_file"], "deny": ["exec"]},
            }
        }
    }

    parsed = Config.model_validate(convert_keys(raw))
    assert parsed.tools.policy.default_deny_tools == ["exec"]
    assert parsed.tools.policy.kill_switch_enabled is True
    assert parsed.tools.policy.kill_switch_reason == "incident-response"
    assert parsed.tools.policy.cron_allowed_channels == ["cli", "telegram"]
    assert parsed.tools.policy.channel_policies["discord"].deny == ["exec", "spawn"]
    assert parsed.tools.policy.channel_policies["cli"].allow == ["*"]
    assert parsed.tools.policy.agent.allow == ["read_file"]
    assert parsed.tools.policy.agent.deny == ["web_fetch"]
    assert parsed.tools.policy.subagent.deny == ["exec"]


def test_tools_policy_normalizes_case_whitespace_and_duplicates() -> None:
    raw = {
        "tools": {
            "policy": {
                "defaultDenyTools": [" Exec ", "SPAWN", "exec", " "],
                "killSwitchReason": "  planned-maintenance  ",
                "cronAllowedChannels": [" Discord ", "discord", "CLI"],
                "channelPolicies": {
                    " Discord ": {"deny": [" Exec ", ""]},
                },
                "agent": {"allow": [" READ_FILE ", "read_file"], "deny": [" WEB_FETCH "]},
            }
        }
    }

    parsed = Config.model_validate(convert_keys(raw))
    assert parsed.tools.policy.default_deny_tools == ["exec", "spawn"]
    assert parsed.tools.policy.kill_switch_reason == "planned-maintenance"
    assert parsed.tools.policy.cron_allowed_channels == ["discord", "cli"]
    assert "discord" in parsed.tools.policy.channel_policies
    assert parsed.tools.policy.channel_policies["discord"].deny == ["exec"]
    assert parsed.tools.policy.agent.allow == ["read_file"]
    assert parsed.tools.policy.agent.deny == ["web_fetch"]


def test_tools_policy_merges_duplicate_channel_policy_keys() -> None:
    raw = {
        "tools": {
            "policy": {
                "channelPolicies": {
                    "discord": {"deny": ["exec"], "allow": ["read_file"]},
                    "_discord": {"deny": ["spawn"], "allow": ["web_search"]},
                }
            }
        }
    }
    parsed = Config.model_validate(convert_keys(raw))
    layer = parsed.tools.policy.channel_policies["discord"]
    assert layer.deny == ["exec", "spawn"]
    assert layer.allow == ["read_file", "web_search"]


