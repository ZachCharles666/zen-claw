from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_channel_rbac_fields_accept_camel_case() -> None:
    raw = {
        "channels": {
            "telegram": {
                "enabled": True,
                "admins": ["A1"],
                "users": ["U1", "U2"],
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.channels.telegram.admins == ["A1"]
    assert cfg.channels.telegram.users == ["U1", "U2"]


def test_channel_rbac_fields_default_empty() -> None:
    cfg = Config()
    assert cfg.channels.telegram.admins == []
    assert cfg.channels.telegram.users == []
    assert cfg.channels.allow_from == []
    assert cfg.channels.deny_from == []
    assert cfg.channels.outbound_rate_limit_by_channel == {}


def test_channel_rate_limit_by_channel_accepts_camel_case() -> None:
    raw = {
        "channels": {
            "outboundRateLimitByChannel": {
                "Discord": {"perSec": 1.5, "burst": 2, "mode": "drop"}
            }
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert "discord" in cfg.channels.outbound_rate_limit_by_channel
    override = cfg.channels.outbound_rate_limit_by_channel["discord"]
    assert override.per_sec == 1.5
    assert override.burst == 2
    assert override.mode == "drop"
