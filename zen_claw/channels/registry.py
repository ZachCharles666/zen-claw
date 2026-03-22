"""Shared channel registry and operator-facing metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

ConfigSummaryFormatter = Callable[[Any], str]


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    display_name: str
    config_attr: str
    module_path: str
    class_name: str
    config_summary: ConfigSummaryFormatter
    transport: str
    inbound_mode: str
    needs_groq_api_key: bool = False
    webhook_slot: str | None = None
    passive_capable: bool = False
    runtime_actions: tuple[str, ...] = ()


def _fmt_not_configured() -> str:
    return "[dim]not configured[/dim]"


def _mask_token(value: str, *, prefix: str) -> str:
    token = str(value or "").strip()
    if not token:
        return _fmt_not_configured()
    return f"{prefix}: {token[:10]}..."


def _field_summary(field: str, *, prefix: str = "") -> ConfigSummaryFormatter:
    def _formatter(cfg: Any) -> str:
        value = str(getattr(cfg, field, "") or "").strip()
        if not value:
            return _fmt_not_configured()
        return f"{prefix}{value}" if prefix else value

    return _formatter


def _token_summary(field: str, *, prefix: str) -> ConfigSummaryFormatter:
    def _formatter(cfg: Any) -> str:
        return _mask_token(getattr(cfg, field, ""), prefix=prefix)

    return _formatter


CHANNEL_SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        key="whatsapp",
        display_name="WhatsApp",
        config_attr="whatsapp",
        module_path="zen_claw.channels.whatsapp",
        class_name="WhatsAppChannel",
        config_summary=_field_summary("bridge_url"),
        transport="websocket_bridge",
        inbound_mode="bridge",
    ),
    ChannelSpec(
        key="discord",
        display_name="Discord",
        config_attr="discord",
        module_path="zen_claw.channels.discord",
        class_name="DiscordChannel",
        config_summary=_field_summary("gateway_url"),
        transport="websocket",
        inbound_mode="gateway",
        needs_groq_api_key=True,
    ),
    ChannelSpec(
        key="telegram",
        display_name="Telegram",
        config_attr="telegram",
        module_path="zen_claw.channels.telegram",
        class_name="TelegramChannel",
        config_summary=_token_summary("token", prefix="token"),
        transport="polling_http",
        inbound_mode="polling",
        needs_groq_api_key=True,
    ),
    ChannelSpec(
        key="webchat",
        display_name="WebChat",
        config_attr="webchat",
        module_path="zen_claw.channels.webchat",
        class_name="WebChatChannel",
        config_summary=_token_summary("token", prefix="token"),
        transport="local_web",
        inbound_mode="http",
        runtime_actions=("start", "stop", "restart", "apply"),
    ),
    ChannelSpec(
        key="webhook_trigger",
        display_name="Webhook Trigger",
        config_attr="webhook_trigger",
        module_path="zen_claw.channels.webhook_trigger",
        class_name="WebhookTriggerChannel",
        config_summary=_field_summary("cron_target_url"),
        transport="http_webhook",
        inbound_mode="webhook",
        webhook_slot="webhook_trigger",
    ),
    ChannelSpec(
        key="slack",
        display_name="Slack",
        config_attr="slack",
        module_path="zen_claw.channels.slack",
        class_name="SlackChannel",
        config_summary=_token_summary("bot_token", prefix="bot_token"),
        transport="socket_or_http",
        inbound_mode="socket_mode_or_webhook",
        webhook_slot="slack",
        passive_capable=True,
    ),
    ChannelSpec(
        key="signal",
        display_name="Signal",
        config_attr="signal",
        module_path="zen_claw.channels.signal",
        class_name="SignalChannel",
        config_summary=_field_summary("mode", prefix="mode: "),
        transport="http_bridge_or_cli",
        inbound_mode="polling",
    ),
    ChannelSpec(
        key="matrix",
        display_name="Matrix",
        config_attr="matrix",
        module_path="zen_claw.channels.matrix",
        class_name="MatrixChannel",
        config_summary=_field_summary("homeserver"),
        transport="matrix_client",
        inbound_mode="sync",
    ),
    ChannelSpec(
        key="feishu",
        display_name="Feishu",
        config_attr="feishu",
        module_path="zen_claw.channels.feishu",
        class_name="FeishuChannel",
        config_summary=_field_summary("app_id", prefix="app_id: "),
        transport="websocket",
        inbound_mode="long_connection",
    ),
    ChannelSpec(
        key="wechat_mp",
        display_name="WeChat MP",
        config_attr="wechat_mp",
        module_path="zen_claw.channels.wechat_mp",
        class_name="WechatMPChannel",
        config_summary=_field_summary("app_id", prefix="app_id: "),
        transport="http_webhook",
        inbound_mode="webhook",
        webhook_slot="wechat",
    ),
    ChannelSpec(
        key="wecom",
        display_name="WeCom",
        config_attr="wecom",
        module_path="zen_claw.channels.wecom",
        class_name="WeComChannel",
        config_summary=_field_summary("corp_id", prefix="corp_id: "),
        transport="http_webhook",
        inbound_mode="webhook",
        webhook_slot="wecom",
    ),
    ChannelSpec(
        key="dingtalk",
        display_name="DingTalk",
        config_attr="dingtalk",
        module_path="zen_claw.channels.dingtalk",
        class_name="DingTalkChannel",
        config_summary=_field_summary("webhook_url"),
        transport="websocket_or_http",
        inbound_mode="stream_or_webhook",
        webhook_slot="dingtalk",
        passive_capable=True,
    ),
)


def iter_channel_specs() -> tuple[ChannelSpec, ...]:
    return CHANNEL_SPECS


def get_channel_config(config: Any, spec: ChannelSpec) -> Any:
    channels_cfg = getattr(config, "channels", SimpleNamespace())
    fallback = SimpleNamespace(enabled=False, admins=[], users=[], allow_from=[])
    return getattr(channels_cfg, spec.config_attr, fallback)


def build_channel_rbac_row(config: Any, spec: ChannelSpec) -> dict[str, Any]:
    ch_cfg = get_channel_config(config, spec)
    admins = sorted({str(v).strip() for v in getattr(ch_cfg, "admins", []) if str(v).strip()})
    users = sorted({str(v).strip() for v in getattr(ch_cfg, "users", []) if str(v).strip()})
    return {
        "name": spec.key,
        "display_name": spec.display_name,
        "enabled": bool(getattr(ch_cfg, "enabled", False)),
        "rbac_enabled": bool(admins or users),
        "admins": len(admins),
        "users": len(users),
        "admins_list": admins,
        "users_list": users,
        "configuration": spec.config_summary(ch_cfg),
        "transport": spec.transport,
        "inbound_mode": spec.inbound_mode,
        "webhook_backed": bool(spec.webhook_slot),
        "passive_capable": bool(spec.passive_capable),
        "runtime_supported": bool(spec.runtime_actions),
        "runtime_actions": list(spec.runtime_actions),
        "runtime_apply_supported": "apply" in spec.runtime_actions,
    }
