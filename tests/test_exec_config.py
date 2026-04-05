from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_exec_config_accepts_sidecar_camel_case() -> None:
    raw = {
        "tools": {
            "policy": {"legacyCompat": True, "productionHardening": False},
            "network": {
                "exec": {
                    "timeout": 15,
                    "mode": "sidecar",
                    "sidecarUrl": "http://127.0.0.1:4488/v1/exec",
                    "sidecarApprovalToken": "abc",
                    "sidecarFallbackToLocal": True,
                    "sidecarHealthcheck": True,
                }
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.tools.network.exec.timeout == 15
    assert config.tools.network.exec.mode == "sidecar"
    assert config.tools.network.exec.sidecar_url.endswith("/v1/exec")
    assert config.tools.network.exec.sidecar_approval_token.get_secret_value() == "abc"
    assert config.tools.network.exec.sidecar_fallback_to_local is True
    assert config.tools.network.exec.sidecar_healthcheck is True
    assert config.tools.effective_exec().mode == "sidecar"
