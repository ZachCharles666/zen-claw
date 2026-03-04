from zen_claw.config.loader import convert_keys, convert_to_camel
from zen_claw.config.schema import Config


def test_memory_recall_mode_loads_from_camel_case_config() -> None:
    raw = {
        "agents": {
            "defaults": {
                "memoryRecallMode": "none",
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.agents.defaults.memory_recall_mode == "none"


def test_memory_recall_mode_accepts_recent() -> None:
    raw = {
        "agents": {
            "defaults": {
                "memoryRecallMode": "recent",
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.agents.defaults.memory_recall_mode == "recent"


def test_memory_recall_mode_serializes_to_camel_case() -> None:
    cfg = Config()
    cfg.agents.defaults.memory_recall_mode = "none"
    cfg.agents.defaults.enable_planning = False
    cfg.agents.defaults.max_reflections = 3
    cfg.agents.defaults.auto_parameter_rewrite = True
    cfg.agents.defaults.skill_permissions_mode = "enforce"
    data = convert_to_camel(cfg.model_dump())
    assert data["agents"]["defaults"]["memoryRecallMode"] == "none"
    assert data["agents"]["defaults"]["enablePlanning"] is False
    assert data["agents"]["defaults"]["maxReflections"] == 3
    assert data["agents"]["defaults"]["autoParameterRewrite"] is True
    assert data["agents"]["defaults"]["skillPermissionsMode"] == "enforce"


def test_planning_and_reflection_config_loads_from_camel_case() -> None:
    raw = {
        "agents": {
            "defaults": {
                "enablePlanning": False,
                "maxReflections": 2,
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.agents.defaults.enable_planning is False
    assert cfg.agents.defaults.max_reflections == 2


def test_auto_parameter_rewrite_loads_from_camel_case() -> None:
    raw = {
        "agents": {
            "defaults": {
                "autoParameterRewrite": True,
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.agents.defaults.auto_parameter_rewrite is True


def test_skill_permissions_mode_loads_from_camel_case() -> None:
    raw = {
        "agents": {
            "defaults": {
                "skillPermissionsMode": "warn",
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.agents.defaults.skill_permissions_mode == "warn"


def test_cron_policy_fields_load_from_camel_case() -> None:
    raw = {
        "tools": {
            "policy": {
                "cronAllowedChannels": [" Telegram ", "discord", "discord"],
                "cronAllowedActionsByChannel": {
                    " Telegram ": ["list", "remove", "remove", "unknown"],
                },
                "cronRequireRemoveConfirmation": True,
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.tools.policy.cron_allowed_channels == ["telegram", "discord"]
    assert cfg.tools.policy.cron_allowed_actions_by_channel == {"telegram": ["list", "remove"]}
    assert cfg.tools.policy.cron_require_remove_confirmation is True


def test_cron_policy_fields_serialize_to_camel_case() -> None:
    cfg = Config()
    cfg.tools.policy.cron_allowed_channels = ["telegram"]
    cfg.tools.policy.cron_allowed_actions_by_channel = {"telegram": ["add", "list"]}
    cfg.tools.policy.cron_require_remove_confirmation = True
    data = convert_to_camel(cfg.model_dump())
    policy = data["tools"]["policy"]
    assert policy["cronAllowedChannels"] == ["telegram"]
    assert policy["cronAllowedActionsByChannel"] == {"telegram": ["add", "list"]}
    assert policy["cronRequireRemoveConfirmation"] is True


