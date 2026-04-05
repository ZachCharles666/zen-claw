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


def test_production_hardening_ignores_legacy_subagent_sensitive_override() -> None:
    raw = {
        "tools": {
            "policy": {
                "productionHardening": True,
                "allowSubagentSensitiveTools": True,
            },
            "network": {},
        }
    }
    cfg = Config.model_validate(convert_keys(raw))
    assert cfg.tools.policy.subagent.allow == ["web_search", "web_fetch"]


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


@pytest.mark.parametrize(
    ("path", "raw"),
    [
        (
            "exec",
            {
                "tools": {
                    "policy": {"productionHardening": True},
                    "network": {"exec": {"mode": "sidecar", "sidecarApprovalMode": "token"}},
                }
            },
        ),
        (
            "search",
            {
                "tools": {
                    "policy": {"productionHardening": True},
                    "network": {"search": {"mode": "proxy", "proxyApprovalMode": "token"}},
                }
            },
        ),
        (
            "fetch",
            {
                "tools": {
                    "policy": {"productionHardening": True},
                    "network": {"fetch": {"mode": "proxy", "proxyApprovalMode": "token"}},
                }
            },
        ),
        (
            "browser",
            {
                "tools": {
                    "policy": {"productionHardening": True},
                    "network": {"browser": {"mode": "sidecar", "sidecarApprovalMode": "token"}},
                }
            },
        ),
        (
            "connector",
            {
                "tools": {
                    "policy": {"productionHardening": True},
                    "network": {"connector": {"mode": "sidecar", "sidecarApprovalMode": "token"}},
                }
            },
        ),
    ],
)
def test_production_hardening_requires_hmac_approval_modes(path: str, raw: dict) -> None:
    with pytest.raises(ValueError, match=path):
        Config.model_validate(convert_keys(raw))


def test_production_hardening_rejects_legacy_compat_override() -> None:
    raw = {
        "tools": {
            "policy": {"productionHardening": True, "legacyCompat": True},
            "network": {},
        }
    }
    with pytest.raises(ValueError, match="legacy_compat"):
        Config.model_validate(convert_keys(raw))
