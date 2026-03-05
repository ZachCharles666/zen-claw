import pytest

from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_production_hardening_rejects_legacy_tool_fields_without_network() -> None:
    raw = {
        "tools": {
            "policy": {"productionHardening": True},
            "exec": {"mode": "sidecar"},
        }
    }
    with pytest.raises(ValueError):
        Config.model_validate(convert_keys(raw))


def test_production_hardening_rejects_subagent_sensitive_override() -> None:
    raw = {
        "tools": {
            "policy": {
                "productionHardening": True,
                "allowSubagentSensitiveTools": True,
            },
            "network": {},
        }
    }
    with pytest.raises(ValueError):
        Config.model_validate(convert_keys(raw))


def test_production_hardening_disables_all_fallbacks() -> None:
    raw = {
        "tools": {
            "policy": {"productionHardening": True},
            "network": {
                "exec": {"mode": "sidecar", "sidecarFallbackToLocal": True},
                "search": {"mode": "proxy", "proxyFallbackToLocal": True},
                "fetch": {"mode": "proxy", "proxyFallbackToLocal": True},
            },
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.tools.network.exec.sidecar_fallback_to_local is False
    assert cfg.tools.network.search.proxy_fallback_to_local is False
    assert cfg.tools.network.fetch.proxy_fallback_to_local is False
