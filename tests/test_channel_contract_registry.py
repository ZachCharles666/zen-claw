from zen_claw.channels.registry import ChannelSpec, build_channel_rbac_row, iter_channel_specs
from zen_claw.config.schema import Config


def test_channel_specs_publish_minimum_alpha_contract_metadata() -> None:
    specs = iter_channel_specs()
    assert specs
    for spec in specs:
        assert isinstance(spec, ChannelSpec)
        assert spec.transport
        assert spec.inbound_mode
        assert spec.verify_mode
        assert spec.media_mode
        assert spec.error_mode


def test_build_channel_rbac_row_exposes_contract_fields() -> None:
    cfg = Config()
    cfg.channels.slack.enabled = True
    spec = next(item for item in iter_channel_specs() if item.key == "slack")

    row = build_channel_rbac_row(cfg, spec)

    assert row["name"] == "slack"
    assert row["verify_mode"] == "socket_mode_or_webhook_signature"
    assert row["verify_supported"] is True
    assert row["media_mode"] == "text_and_attachments"
    assert row["media_supported"] is True
    assert row["error_mode"] == "logged_runtime_exception"
    assert row["runtime_visibility_supported"] is True
    assert row["runtime_control_mode"] == "audit_only"
    assert row["alpha_contract_ready"] is True
    assert row["alpha_contract_missing"] == []
    assert row["alpha_contract_support"]["inbound_parsing"] is True
    assert row["alpha_contract_support"]["outbound_sending"] is True
    assert row["alpha_contract_support"]["auth_verification"] is True
    assert row["alpha_contract_support"]["attachment_media"] is True
    assert row["alpha_contract_support"]["error_semantics"] is True
    assert row["alpha_contract_support"]["runtime_status_visibility"] is True
