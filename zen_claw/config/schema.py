"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    allow_from: list[str] = Field(default_factory=list)  # Legacy allow list
    admins: list[str] = Field(default_factory=list)  # RBAC admins
    users: list[str] = Field(default_factory=list)  # RBAC users
    agent_profile: str = "default"


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Legacy allow list
    admins: list[str] = Field(default_factory=list)  # RBAC admins
    users: list[str] = Field(default_factory=list)  # RBAC users
    agent_profile: str = "default"
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Legacy allow list
    admins: list[str] = Field(default_factory=list)  # RBAC admins
    users: list[str] = Field(default_factory=list)  # RBAC users
    agent_profile: str = "default"


class WechatMPConfig(BaseModel):
    """WeChat Official Account (公众号) configuration."""

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    token: str = ""
    encoding_aes_key: str = ""
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class WeComConfig(BaseModel):
    """Enterprise WeChat configuration."""

    enabled: bool = False
    corp_id: str = ""
    corp_secret: str = ""
    agent_id: int = 0
    token: str = ""
    encoding_aes_key: str = ""
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class DingTalkConfig(BaseModel):
    """DingTalk bot configuration."""

    enabled: bool = False
    webhook_url: str = ""
    secret: str = ""
    app_key: str = ""
    app_secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class DiscordConfig(BaseModel):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Legacy allow list
    admins: list[str] = Field(default_factory=list)  # RBAC admins
    users: list[str] = Field(default_factory=list)  # RBAC users
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    agent_profile: str = "default"


class WebChatConfig(BaseModel):
    """Web chat channel configuration."""

    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class WebhookTriggerConfig(BaseModel):
    """Generic webhook trigger channel configuration."""

    enabled: bool = False
    secret: str = ""
    api_key: str = ""
    ip_allowlist: list[str] = Field(default_factory=list)
    timestamp_tolerance_sec: int = 300
    nonce_ttl_sec: int = 600
    allow_unsigned_from_allowlist: bool = True
    cron_target_url: str = ""
    cron_target_timeout_sec: int = 10
    agent_profile: str = "default"


class SlackConfig(BaseModel):
    """Slack channel configuration."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    socket_mode: bool = True
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class SignalConfig(BaseModel):
    """Signal channel configuration."""

    enabled: bool = False
    mode: Literal["signald", "signal_cli"] = "signald"
    signald_url: str = "http://127.0.0.1:8080"
    signald_rpc_path: str = "/api/v1/rpc"
    signal_cli_bin: str = "signal-cli"
    account: str = ""
    attachment_download: bool = True
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class MatrixConfig(BaseModel):
    """Matrix channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    username: str = ""
    password: str = ""
    user_id: str = ""
    access_token: str = ""
    device_id: str = ""
    device_name: str = "zen-claw"
    auto_login: bool = True
    auto_register: bool = False
    e2ee_enabled: bool = False
    e2ee_require: bool = False
    media_download: bool = True
    allow_from: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    agent_profile: str = "default"


class ChannelRateLimitConfig(BaseModel):
    """Per-channel outbound rate-limit override."""

    per_sec: float | None = None
    burst: int | None = None
    mode: Literal["delay", "drop"] | None = None


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""

    allow_from: list[str] = Field(default_factory=list)  # Global allow list (all channels)
    deny_from: list[str] = Field(default_factory=list)  # Global deny list (all channels)
    outbound_rate_limit_per_sec: float = 2.0
    outbound_rate_limit_burst: int = 5
    outbound_rate_limit_mode: Literal["delay", "drop"] = "delay"
    outbound_rate_limit_drop_notice: bool = False
    outbound_rate_limit_drop_notice_cooldown_sec: int = 30
    outbound_rate_limit_drop_notice_text: str = "System busy, please retry shortly."
    outbound_rate_limit_by_channel: dict[str, ChannelRateLimitConfig] = Field(default_factory=dict)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    webchat: WebChatConfig = Field(default_factory=WebChatConfig)
    webhook_trigger: WebhookTriggerConfig = Field(default_factory=WebhookTriggerConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    wechat_mp: WechatMPConfig = Field(default_factory=WechatMPConfig)
    wecom: WeComConfig = Field(default_factory=WeComConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)

    @model_validator(mode="after")
    def _normalize_channel_rate_limit_keys(self) -> "ChannelsConfig":
        normalized: dict[str, ChannelRateLimitConfig] = {}
        for key, value in self.outbound_rate_limit_by_channel.items():
            k = str(key).strip().lower()
            if not k:
                continue
            normalized[k] = value
        self.outbound_rate_limit_by_channel = normalized
        return self


class AgentDefaults(BaseModel):
    """Default agent configuration."""

    workspace: str = "~/.zen-claw/workspace"
    model: str = "anthropic/claude-opus-4-5"
    vision_model: str = ""
    memory_recall_mode: Literal["keyword", "recent", "sqlite", "rag", "none"] = "sqlite"
    enable_planning: bool = True
    max_reflections: int = 1
    auto_parameter_rewrite: bool = False
    skill_permissions_mode: Literal["off", "warn", "enforce"] = "off"
    max_tokens: int = 8192
    compression_trigger_ratio: float = 0.8
    compression_hysteresis_ratio: float = 0.5
    compression_cooldown_turns: int = 5
    temperature: float = 0.7
    max_tool_iterations: int = 20
    allowed_models: list[str] = Field(default_factory=list)


class SocialAgentConfig(BaseModel):
    """Configuration for autonomous social agent loop."""

    platform: str = "moltbook"
    platform_base_url: str = ""
    submolt: str = ""
    max_posts_per_cycle: int = 10
    dry_run: bool = False
    interval_sec: int = 3600
    credential_key: str = ""
    system_prompt_override: str = ""
    enable_like: bool = True


class AgentIdentityConfig(BaseModel):
    """Configuration for agent cryptographic identity."""

    key_dir: str = ""


class AgentsConfig(BaseModel):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    social: SocialAgentConfig = Field(default_factory=SocialAgentConfig)
    identity: AgentIdentityConfig = Field(default_factory=AgentIdentityConfig)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    rate_limit_delay_sec: float = (
        0.0  # Optional delay before each request (e.g. for free tier API limits)
    )


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""

    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    tts: Literal["edge", "minimax", "openai", "off"] = "edge"
    tts_default_voice: str = "zh-CN-XiaoxiaoNeural"
    minimax_api_key: str = ""
    minimax_group_id: str = ""


class KnowledgeConfig(BaseModel):
    """Knowledge RAG configuration."""

    enabled: bool = True
    default_notebook: str = "default"
    embedding_provider: Literal["local", "openai"] = "local"
    embedding_model: str = "BAAI/bge-m3"


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class ApiGatewayConfig(BaseModel):
    """REST API Gateway configuration."""

    enabled: bool = False
    rate_limit_per_minute: int = 60
    api_keys_env_var: str = "ZEN_CLAW_API_KEYS"


class MultiTenantConfig(BaseModel):
    """Multi-tenant mode configuration."""

    enabled: bool = False
    jwt_secret: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 86400
    session_cookie_name: str = "nc_session"
    session_cookie_secure: bool = True
    login_path: str = "/login"
    public_paths: list[str] = Field(
        default_factory=lambda: ["/login", "/api/v1/health", "/static/"]
    )


class SkillsMarketConfig(BaseModel):
    """Skills market configuration."""

    registry_url: str = "https://zen-claw.github.io/skills-registry/index.json"
    cache_file: str = "registry_cache.json"
    cache_ttl_sec: int = 3600
    publish_output_dir: str = "dist"
    publish_require_integrity: bool = True
    trusted_hosts: list[str] = Field(default_factory=list)


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    api_key: str = ""  # Brave Search API key
    max_results: int = 5
    mode: Literal["local", "proxy"] = "local"
    proxy_url: str = "http://127.0.0.1:4499/v1/search"
    proxy_healthcheck: bool = False
    proxy_fallback_to_local: bool = False


class WebFetchConfig(BaseModel):
    """Web fetch tool configuration."""

    mode: Literal["local", "proxy"] = "local"
    proxy_url: str = "http://127.0.0.1:4499/v1/fetch"
    proxy_healthcheck: bool = False
    proxy_fallback_to_local: bool = False


class WebToolsConfig(BaseModel):
    """Web tools configuration."""

    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)


class BrowserToolConfig(BaseModel):
    """Browser automation configuration."""

    mode: Literal["off", "sidecar"] = "off"
    sidecar_url: str = "http://127.0.0.1:4500/v1/browser"
    sidecar_approval_token: SecretStr = SecretStr("")
    sidecar_healthcheck: bool = False
    sidecar_fallback_to_off: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_steps: int = 20
    timeout_sec: int = 30


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""

    timeout: int = 60
    mode: Literal["local", "sidecar"] = "local"
    sidecar_url: str = "http://127.0.0.1:4488/v1/exec"
    sidecar_approval_mode: Literal["token", "hmac"] = "token"
    sidecar_approval_token: SecretStr = SecretStr("")
    sidecar_fallback_to_local: bool = False
    sidecar_healthcheck: bool = False


class ToolPolicyLayerConfig(BaseModel):
    """Per-scope tool policy layer."""

    allow: list[str] | None = None
    deny: list[str] | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "ToolPolicyLayerConfig":
        self.allow = self._normalize_tool_list(self.allow)
        self.deny = self._normalize_tool_list(self.deny)
        return self

    @staticmethod
    def _normalize_tool_list(values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for v in values:
            token = v.strip().lower()
            if not token:
                continue
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out


class ToolPolicyConfig(BaseModel):
    """Tool policy configuration."""

    default_deny_tools: list[str] = Field(default_factory=lambda: ["exec", "spawn"])
    kill_switch_enabled: bool = False
    kill_switch_reason: str = ""
    allow_subagent_sensitive_tools: bool = False
    hitl_sensitive_tools: list[str] | None = None  # Explicit override of HitL sensitive tool gate
    max_jobs_per_session: int = 10
    cron_allowed_channels: list[str] = Field(default_factory=list)
    cron_allowed_actions_by_channel: dict[str, list[str]] = Field(default_factory=dict)
    cron_require_remove_confirmation: bool = False
    channel_policies: dict[str, ToolPolicyLayerConfig] = Field(default_factory=dict)
    production_hardening: bool = False
    agent: ToolPolicyLayerConfig = Field(default_factory=lambda: ToolPolicyLayerConfig(allow=["*"]))
    subagent: ToolPolicyLayerConfig = Field(
        default_factory=lambda: ToolPolicyLayerConfig(
            allow=["read_file", "write_file", "list_dir", "exec", "web_search", "web_fetch"],
            deny=[
                "spawn",
                "message",
                "cron",
                "sessions_spawn",
                "sessions_list",
                "sessions_kill",
                "sessions_read",
                "sessions_write",
                "sessions_signal",
                "sessions_resize",
            ],
        )
    )

    @model_validator(mode="after")
    def _normalize_policy_fields(self) -> "ToolPolicyConfig":
        self.default_deny_tools = (
            ToolPolicyLayerConfig._normalize_tool_list(self.default_deny_tools) or []
        )
        self.kill_switch_reason = (self.kill_switch_reason or "").strip()
        self.cron_allowed_channels = self._normalize_channel_list(self.cron_allowed_channels)
        normalized_actions: dict[str, list[str]] = {}
        for key, actions in self.cron_allowed_actions_by_channel.items():
            k = key.strip().lower().lstrip("_")
            if not k:
                continue
            out: list[str] = []
            seen: set[str] = set()
            for action in actions:
                token = action.strip().lower()
                if token not in {"add", "list", "remove"} or token in seen:
                    continue
                seen.add(token)
                out.append(token)
            if out:
                normalized_actions[k] = out
        self.cron_allowed_actions_by_channel = normalized_actions
        normalized: dict[str, ToolPolicyLayerConfig] = {}
        for key, layer in self.channel_policies.items():
            k = key.strip().lower().lstrip("_")
            if not k:
                continue
            if k in normalized:
                prev = normalized[k]
                normalized[k] = ToolPolicyLayerConfig(
                    allow=self._merge_policy_list(prev.allow, layer.allow),
                    deny=self._merge_policy_list(prev.deny, layer.deny),
                )
            else:
                normalized[k] = layer
        self.channel_policies = normalized
        return self

    @staticmethod
    def _normalize_channel_list(values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for v in values:
            token = v.strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    @staticmethod
    def _merge_policy_list(a: list[str] | None, b: list[str] | None) -> list[str] | None:
        if a is None and b is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for source in (a or []), (b or []):
            for item in source:
                token = item.strip().lower()
                if not token or token in seen:
                    continue
                seen.add(token)
                out.append(token)
        return out


class NetworkToolsConfig(BaseModel):
    """Unified network/sidecar configuration surface."""

    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)
    browser: BrowserToolConfig = Field(default_factory=BrowserToolConfig)


class ToolsConfig(BaseModel):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    network: NetworkToolsConfig = Field(default_factory=NetworkToolsConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    sidecar_supervisor: bool = False  # If true, attempt to start and supervise sidecars locally
    sidecar_supervisor_fail_window_sec: int = 120
    sidecar_supervisor_fail_threshold: int = 5
    sidecar_supervisor_circuit_open_sec: int = 120
    policy: ToolPolicyConfig = Field(default_factory=ToolPolicyConfig)

    @model_validator(mode="after")
    def _backfill_network_from_legacy_when_missing(self) -> "ToolsConfig":
        """
        Keep direct model validation backward-compatible with legacy fields.

        If `tools.network` is not explicitly provided, legacy values are promoted
        into the effective network config.
        """
        if "network" in self.model_fields_set:
            return self

        default_exec = ExecToolConfig()
        default_search = WebSearchConfig()
        default_fetch = WebFetchConfig()

        if self.network.exec == default_exec and self.exec != default_exec:
            self.network.exec = self.exec
        if self.network.search == default_search and self.web.search != default_search:
            self.network.search = self.web.search
        if self.network.fetch == default_fetch and self.web.fetch != default_fetch:
            self.network.fetch = self.web.fetch
        return self

    @model_validator(mode="after")
    def _enforce_production_hardening(self) -> "ToolsConfig":
        """Apply strict production hardening constraints when enabled."""
        if not self.policy.production_hardening:
            return self

        # In production hardening mode, require canonical config surface.
        if "network" not in self.model_fields_set and (
            "exec" in self.model_fields_set or "web" in self.model_fields_set
        ):
            raise ValueError(
                "production_hardening requires tools.network.*; "
                "legacy tools.exec/tools.web.* are not allowed"
            )

        # Prevent disabling subagent hard guardrail.
        if self.policy.allow_subagent_sensitive_tools:
            raise ValueError("production_hardening forbids allowSubagentSensitiveTools=true")

        # Disable all local fallback paths in strict mode.
        self.network.exec.sidecar_fallback_to_local = False
        self.network.search.proxy_fallback_to_local = False
        self.network.fetch.proxy_fallback_to_local = False
        self.network.browser.sidecar_fallback_to_off = False
        return self

    def effective_exec(self) -> ExecToolConfig:
        """Get effective exec configuration (network scope takes precedence)."""
        return self.network.exec

    def effective_search(self) -> WebSearchConfig:
        """Get effective web search configuration (network scope takes precedence)."""
        return self.network.search

    def effective_fetch(self) -> WebFetchConfig:
        """Get effective web fetch configuration (network scope takes precedence)."""
        return self.network.fetch

    def effective_browser(self) -> BrowserToolConfig:
        """Get effective browser configuration."""
        return self.network.browser


class Config(BaseSettings):
    """Root configuration for zen_claw."""

    model_config = SettingsConfigDict(
        env_prefix="ZEN_CLAW_",
        env_nested_delimiter="__",
    )

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    api_gateway: ApiGatewayConfig = Field(default_factory=ApiGatewayConfig)
    multitenant: MultiTenantConfig = Field(default_factory=MultiTenantConfig)
    skills_market: SkillsMarketConfig = Field(default_factory=SkillsMarketConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    # Default base URLs for API gateways
    _GATEWAY_DEFAULTS = {
        "openrouter": "https://openrouter.ai/api/v1",
        "aihubmix": "https://aihubmix.com/v1",
    }

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        model = (model or self.agents.defaults.model).lower()
        p = self.providers
        # Keyword �?provider mapping (order matters: gateways first)
        keyword_map = {
            "aihubmix": p.aihubmix,
            "openrouter": p.openrouter,
            "deepseek": p.deepseek,
            "anthropic": p.anthropic,
            "claude": p.anthropic,
            "openai": p.openai,
            "gpt": p.openai,
            "gemini": p.gemini,
            "zhipu": p.zhipu,
            "glm": p.zhipu,
            "zai": p.zhipu,
            "dashscope": p.dashscope,
            "qwen": p.dashscope,
            "groq": p.groq,
            "moonshot": p.moonshot,
            "kimi": p.moonshot,
            "vllm": p.vllm,
        }
        for kw, provider in keyword_map.items():
            if kw in model and provider.api_key:
                return provider
        # Fallback: gateways first (can serve any model), then specific providers
        all_providers = [
            p.openrouter,
            p.aihubmix,
            p.anthropic,
            p.openai,
            p.deepseek,
            p.gemini,
            p.zhipu,
            p.dashscope,
            p.moonshot,
            p.vllm,
            p.groq,
        ]
        return next((pr for pr in all_providers if pr.api_key), None)

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for known gateways."""
        p = self.get_provider(model)
        if p and p.api_base:
            return p.api_base
        # Default URLs for known gateways (openrouter, aihubmix)
        for name, url in self._GATEWAY_DEFAULTS.items():
            if p == getattr(self.providers, name):
                return url
        return None
