from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_tools_network_config_parses_camel_case() -> None:
    raw = {
        "tools": {
            "network": {
                "exec": {
                    "mode": "sidecar",
                    "sidecarUrl": "http://127.0.0.1:4488/v1/exec",
                    "sidecarApprovalMode": "hmac",
                },
                "search": {"mode": "proxy", "proxyUrl": "http://127.0.0.1:4499/v1/search"},
                "fetch": {"mode": "proxy", "proxyUrl": "http://127.0.0.1:4499/v1/fetch"},
                "browser": {"mode": "sidecar", "sidecarUrl": "http://127.0.0.1:4500/v1/browser", "maxSteps": 10},
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.network is not None
    assert config.tools.network.exec.mode == "sidecar"
    assert config.tools.network.exec.sidecar_approval_mode == "hmac"
    assert config.tools.network.search.mode == "proxy"
    assert config.tools.network.fetch.mode == "proxy"
    assert config.tools.network.browser.mode == "sidecar"
    assert config.tools.network.browser.max_steps == 10


def test_tools_network_takes_precedence_over_legacy_fields() -> None:
    raw = {
        "tools": {
            "exec": {"mode": "local"},
            "web": {
                "search": {"mode": "local"},
                "fetch": {"mode": "local"},
            },
            "network": {
                "exec": {"mode": "sidecar"},
                "search": {"mode": "proxy"},
                "fetch": {"mode": "proxy"},
                "browser": {"mode": "sidecar"},
            },
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.effective_exec().mode == "sidecar"
    assert config.tools.effective_search().mode == "proxy"
    assert config.tools.effective_fetch().mode == "proxy"
    assert config.tools.effective_browser().mode == "sidecar"


def test_tools_network_is_canonical_by_default() -> None:
    config = Config()
    assert config.tools.network is not None
    assert config.tools.effective_exec().mode == config.tools.network.exec.mode
    assert config.tools.effective_search().mode == config.tools.network.search.mode
    assert config.tools.effective_fetch().mode == config.tools.network.fetch.mode
    assert config.tools.effective_browser().mode == config.tools.network.browser.mode


