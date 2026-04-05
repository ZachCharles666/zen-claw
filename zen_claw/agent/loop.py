"""Agent loop: the core processing engine."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zen_claw.config.schema import (
        BrowserToolConfig,
        ConnectorToolConfig,
        ExecToolConfig,
        ToolPolicyConfig,
        WebFetchConfig,
        WebSearchConfig,
    )
    from zen_claw.cron.service import CronService
    from zen_claw.observability.audit import AuditWorker
    from zen_claw.session.manager import Session

from loguru import logger

from zen_claw.agent.approval_gate import ApprovalGate, ApprovalStatus
from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.context_compression import ContextCompressor
from zen_claw.agent.execution import ExecutionController
from zen_claw.agent.intent_router import (
    IntentRouter,
    IntentRouteResult,
    IntentToolContract,
)
from zen_claw.agent.intent_router_classifier import IntentRouterClassifier
from zen_claw.agent.memory_extractor import MemoryExtractor
from zen_claw.agent.orchestration import (
    RouteCandidate,
    build_arbitration_decision,
    build_execution_intent,
    build_routing_decision,
    resolve_routing_stage,
)
from zen_claw.agent.subagent import SubagentManager
from zen_claw.agent.tools.browser import (
    BrowserClickTool,
    BrowserExtractTool,
    BrowserLoadSessionTool,
    BrowserOpenTool,
    BrowserSaveSessionTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
)
from zen_claw.agent.tools.credentials import CredentialGetTool, CredentialStoreTool
from zen_claw.agent.tools.cron import CronTool
from zen_claw.agent.tools.database import (
    DatabaseExecuteTool,
    DatabaseInspectTool,
    DatabaseMigrateTool,
    DatabaseQueryTool,
    DatabaseSchemaTool,
)
from zen_claw.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from zen_claw.agent.tools.gateway import GatewayTool, GatewayToolLocalStub
from zen_claw.agent.tools.identity import AgentPublicKeyTool, AgentSignTool, AgentVerifyTool
from zen_claw.agent.tools.knowledge import KnowledgeAddTool, KnowledgeListTool, KnowledgeSearchTool
from zen_claw.agent.tools.message import MessageTool
from zen_claw.agent.tools.policy import ToolPolicyEngine
from zen_claw.agent.tools.registry import ToolRegistry
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.agent.tools.service import ServiceStartTool, ServiceStatusTool, ServiceStopTool
from zen_claw.agent.tools.sessions import (
    SessionsKillTool,
    SessionsListTool,
    SessionsReadTool,
    SessionsResizeTool,
    SessionsSignalTool,
    SessionsSpawnTool,
    SessionsWriteTool,
)
from zen_claw.agent.tools.shell import ExecTool
from zen_claw.agent.tools.social_platform import (
    SocialPlatformGetTool,
    SocialPlatformLikeTool,
    SocialPlatformPostTool,
)
from zen_claw.agent.tools.spawn import SpawnTool
from zen_claw.agent.tools.tts import TextToSpeechTool
from zen_claw.agent.tools.web import WebFetchTool, WebSearchTool
from zen_claw.bus.events import InboundMessage, OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.errors import AgentMidTurnReloadException
from zen_claw.observability.trace import TraceContext
from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.security_context import (
    build_security_context,
    is_trusted_local_surface,
    normalize_workspace_id,
    security_policy_snapshot,
)
from zen_claw.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        system_prompt_override: str | None = None,
        system_prompt_file: str | None = None,
        profile_allowed_tools: list[str] | None = None,
        profile_denied_tools: list[str] | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        web_search_config: "WebSearchConfig | None" = None,
        web_fetch_config: "WebFetchConfig | None" = None,
        connector_config: "ConnectorToolConfig | None" = None,
        browser_config: "BrowserToolConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        tool_policy_config: "ToolPolicyConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        memory_recall_mode: str = "sqlite",
        enable_planning: bool = True,
        max_reflections: int = 1,
        auto_parameter_rewrite: bool = False,
        max_context_tokens: int = 8192,
        compression_trigger_ratio: float = 0.8,
        compression_hysteresis_ratio: float = 0.5,
        compression_cooldown_turns: int = 5,
        vision_model: str | None = None,
        thinking_model: str | None = None,
        fallback_model: str | None = None,
        cost_model: str | None = None,
        stability_model: str | None = None,
        intent_model_overrides: dict[str, str] | None = None,
        task_type_model_overrides: dict[str, str] | None = None,
        skill_names: list[str] | None = None,
        skill_permissions_mode: str = "off",  # off|warn|enforce
        allowed_models: list[str] | None = None,
        audit_worker: "AuditWorker | None" = None,
        agent_id: str = "default",
        dev_profile: bool = False,
        trusted_local_only: bool = False,
    ):
        from zen_claw.config.schema import (
            BrowserToolConfig,
            ConnectorToolConfig,
            ExecToolConfig,
            ToolPolicyConfig,
            WebFetchConfig,
            WebSearchConfig,
        )

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.agent_id = str(agent_id or "default").strip().lower() or "default"
        self.dev_profile = bool(dev_profile)
        self.trusted_local_only = bool(trusted_local_only)
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_fetch_config = web_fetch_config or WebFetchConfig()
        self.connector_config = connector_config or ConnectorToolConfig()
        self.browser_config = browser_config or BrowserToolConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.tool_policy_config = tool_policy_config or ToolPolicyConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.memory_recall_mode = memory_recall_mode
        self.enable_planning = enable_planning
        self.max_reflections = max_reflections
        self.auto_parameter_rewrite = auto_parameter_rewrite
        self.max_context_tokens = max_context_tokens
        self.compression_trigger_ratio = min(0.99, max(0.1, float(compression_trigger_ratio)))
        self.compression_hysteresis_ratio = min(
            self.compression_trigger_ratio,
            max(0.05, float(compression_hysteresis_ratio)),
        )
        self.compression_cooldown_turns = max(0, int(compression_cooldown_turns))
        self.allowed_models: list[str] = [m.lower().strip() for m in (allowed_models or [])]
        self.vision_model = (vision_model or "").strip()
        self.thinking_model = (thinking_model or "").strip()
        self.fallback_model = (fallback_model or "").strip()
        self.cost_model = (cost_model or "").strip()
        self.stability_model = (stability_model or "").strip()
        self.profile_allowed_tools = [
            str(tool).strip().lower() for tool in (profile_allowed_tools or []) if str(tool).strip()
        ] or None
        self.profile_denied_tools = [
            str(tool).strip().lower() for tool in (profile_denied_tools or []) if str(tool).strip()
        ] or None
        self.intent_model_overrides = {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in (intent_model_overrides or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        self.task_type_model_overrides = {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in (task_type_model_overrides or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        self.skill_names = skill_names or []
        mode = (skill_permissions_mode or "off").strip().lower()
        if (
            self.tool_policy_config
            and self.tool_policy_config.production_hardening
            and self.skill_names
        ):
            # In strict mode, if the user explicitly loads skills into the prompt,
            # enforce declared permissions to avoid accidental tool expansion.
            mode = "enforce"
        self.skill_permissions_mode = mode

        self.context = ContextBuilder(
            workspace,
            memory_recall_mode=self.memory_recall_mode,
            max_tokens=self.max_context_tokens,
            system_prompt_override=system_prompt_override,
            system_prompt_file=system_prompt_file,
        )
        self.sessions = SessionManager(workspace)

        # Approval gate for sensitive tool calls
        try:
            from zen_claw.config.loader import get_data_dir

            sensitive_list = self.tool_policy_config.hitl_sensitive_tools
            sensitive_set = frozenset(sensitive_list) if sensitive_list is not None else None
            self.approval_gate: ApprovalGate | None = ApprovalGate(
                data_dir=get_data_dir(), sensitive_tools=sensitive_set
            )
        except Exception:
            self.approval_gate = None

        self._audit_worker = audit_worker
        self.tools = ToolRegistry(
            policy=ToolPolicyEngine(
                default_deny_tools=set(self.tool_policy_config.default_deny_tools)
            ),
            audit_worker=audit_worker,
        )
        self.tools.set_security_policy(self.tool_policy_config)
        self.tools.set_kill_switch(
            self.tool_policy_config.kill_switch_enabled,
            reason=self.tool_policy_config.kill_switch_reason,
        )
        self.execution = ExecutionController(
            max_reflections=self.max_reflections,
            enable_planning=self.enable_planning,
        )
        self.intent_router = IntentRouter(
            allow_runtime_constrained_replan=self.enable_planning,
        )
        self.intent_router_classifier = IntentRouterClassifier()
        self.compressor = ContextCompressor(
            trigger_ratio=self.compression_trigger_ratio,
            hysteresis_ratio=self.compression_hysteresis_ratio,
            cooldown_turns=self.compression_cooldown_turns,
        )
        self.memory_extractor = MemoryExtractor()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            web_search_config=self.web_search_config,
            web_fetch_config=self.web_fetch_config,
            exec_config=self.exec_config,
            tool_policy_config=self.tool_policy_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._deferred_retry_delay_sec = 2.0
        self._deferred_retry_tasks: set[asyncio.Task[Any]] = set()
        self._last_run_model_used: str | None = None
        self._last_run_model_reason: str | None = None
        self._last_execution_trace_summary: dict[str, Any] | None = None
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                mode=self.exec_config.mode,
                sidecar_url=self.exec_config.sidecar_url,
                sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                sidecar_fallback_to_local=self.exec_config.sidecar_fallback_to_local,
                sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
            )
        )

        # Gateway tool (for isolated runtimes).
        # In local mode a stub is registered so skills get a clear error instead
        # of a silent "tool not found" failure (LOW-010).
        if self.exec_config.mode == "sidecar":
            gateway_backend = ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                mode=self.exec_config.mode,
                sidecar_url=self.exec_config.sidecar_url,
                sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                sidecar_fallback_to_local=False,  # Strictly enforce sandbox failure if proxy is down
                sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
            )
            self.tools.register(
                GatewayTool(backend_tool=gateway_backend, workspace=str(self.workspace))
            )
        else:
            self.tools.register(GatewayToolLocalStub())

        if self.exec_config.mode == "sidecar":
            self.tools.register(
                SessionsSpawnTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsListTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsKillTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsReadTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsWriteTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsSignalTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )
            self.tools.register(
                SessionsResizeTool(
                    sidecar_exec_url=self.exec_config.sidecar_url,
                    sidecar_approval_mode=self.exec_config.sidecar_approval_mode,
                    sidecar_approval_token=self.exec_config.sidecar_approval_token.get_secret_value(),
                    sidecar_healthcheck=self.exec_config.sidecar_healthcheck,
                )
            )

        # Web tools
        self.tools.register(
            WebSearchTool(
                api_key=self.brave_api_key,
                max_results=self.web_search_config.max_results,
                mode=self.web_search_config.mode,
                proxy_url=self.web_search_config.proxy_url,
                proxy_approval_mode=self.web_search_config.proxy_approval_mode,
                proxy_approval_token=self.web_search_config.proxy_approval_token.get_secret_value(),
                proxy_healthcheck=self.web_search_config.proxy_healthcheck,
                proxy_fallback_to_local=self.web_search_config.proxy_fallback_to_local,
            )
        )
        self.tools.register(
            WebFetchTool(
                mode=self.web_fetch_config.mode,
                proxy_url=self.web_fetch_config.proxy_url,
                proxy_approval_mode=self.web_fetch_config.proxy_approval_mode,
                proxy_approval_token=self.web_fetch_config.proxy_approval_token.get_secret_value(),
                proxy_healthcheck=self.web_fetch_config.proxy_healthcheck,
                proxy_fallback_to_local=self.web_fetch_config.proxy_fallback_to_local,
            )
        )
        if self.browser_config.mode == "sidecar":
            browser_args = dict(
                mode=self.browser_config.mode,
                sidecar_url=self.browser_config.sidecar_url,
                sidecar_approval_mode=self.browser_config.sidecar_approval_mode,
                sidecar_approval_token=self.browser_config.sidecar_approval_token.get_secret_value(),
                sidecar_healthcheck=self.browser_config.sidecar_healthcheck,
                sidecar_fallback_to_off=self.browser_config.sidecar_fallback_to_off,
                allowed_domains=self.browser_config.allowed_domains,
                blocked_domains=self.browser_config.blocked_domains,
                max_steps=self.browser_config.max_steps,
                timeout_sec=self.browser_config.timeout_sec,
            )
            self.tools.register(BrowserOpenTool(**browser_args))
            self.tools.register(BrowserExtractTool(**browser_args))
            self.tools.register(BrowserScreenshotTool(**browser_args))
            self.tools.register(BrowserClickTool(**browser_args))
            self.tools.register(BrowserTypeTool(**browser_args))
            self.tools.register(BrowserSaveSessionTool(**browser_args))
            self.tools.register(BrowserLoadSessionTool(**browser_args))

        # Message tool
        message_tool = MessageTool(
            send_callback=self.bus.publish_outbound,
            connector_proxy_url=self.connector_config.proxy_url,
            connector_approval_mode=self.connector_config.sidecar_approval_mode,
            connector_approval_token=self.connector_config.sidecar_approval_token.get_secret_value(),
            trusted_local_channels=list(self.tool_policy_config.trusted_local_channels),
        )
        self.tools.register(message_tool)

        # Credential vault tools
        self.tools.register(CredentialStoreTool())
        self.tools.register(CredentialGetTool())

        # Social platform API tools
        self.tools.register(SocialPlatformGetTool())
        self.tools.register(
            SocialPlatformPostTool(
                proxy_url=self.connector_config.proxy_url,
                approval_mode=self.connector_config.sidecar_approval_mode,
                approval_token=self.connector_config.sidecar_approval_token.get_secret_value(),
            )
        )
        self.tools.register(
            SocialPlatformLikeTool(
                proxy_url=self.connector_config.proxy_url,
                approval_mode=self.connector_config.sidecar_approval_mode,
                approval_token=self.connector_config.sidecar_approval_token.get_secret_value(),
            )
        )

        # Agent cryptographic identity tools
        self.tools.register(AgentSignTool(workspace=self.workspace))
        self.tools.register(AgentPublicKeyTool(workspace=self.workspace))
        self.tools.register(AgentVerifyTool(workspace=self.workspace))

        # Workspace DB and service lifecycle tools
        self.tools.register(DatabaseQueryTool(workspace=self.workspace))
        self.tools.register(DatabaseExecuteTool(workspace=self.workspace))
        self.tools.register(DatabaseSchemaTool(workspace=self.workspace))
        self.tools.register(DatabaseMigrateTool(workspace=self.workspace))
        self.tools.register(DatabaseInspectTool(workspace=self.workspace))
        self.tools.register(ServiceStartTool(workspace=self.workspace))
        self.tools.register(ServiceStopTool(workspace=self.workspace))
        self.tools.register(ServiceStatusTool(workspace=self.workspace))
        try:
            from zen_claw.config.loader import get_data_dir

            data_dir = get_data_dir()
            self.tools.register(KnowledgeSearchTool(data_dir=data_dir))
            self.tools.register(KnowledgeAddTool(data_dir=data_dir))
            self.tools.register(KnowledgeListTool(data_dir=data_dir))
        except Exception:
            # RAG dependencies are optional.
            pass
        try:
            from zen_claw.agent.tools.business_skills import ComplianceCheckTool, ContentGenTool
            from zen_claw.config.loader import get_data_dir

            self.tools.register(
                ContentGenTool(data_dir=get_data_dir(), provider=self.provider)
            )
            self.tools.register(ComplianceCheckTool(provider=self.provider))
        except Exception:
            # Business skill tools are optional; missing RAG deps should not block startup.
            pass
        try:
            from zen_claw.config.loader import load_config

            tts_cfg = load_config()
            if getattr(tts_cfg.providers, "tts", "edge") != "off":
                self.tools.register(TextToSpeechTool(workspace=self.workspace, config=tts_cfg))
        except Exception:
            pass

        # Daily-assistant tools (email, calendar, gdrive, notion) — all optional
        try:
            from zen_claw.config.loader import load_config as _load_cfg

            _da_cfg = _load_cfg()
            _tools_cfg = _da_cfg.tools

            if _tools_cfg.email.enabled:
                from zen_claw.agent.tools.email import EmailTool

                self.tools.register(EmailTool(config=_tools_cfg.email))

            _cal_cfg = _tools_cfg.calendar
            if _cal_cfg.google.enabled or _cal_cfg.outlook.enabled or _cal_cfg.apple.enabled:
                from zen_claw.agent.tools.calendar import CalendarTool

                self.tools.register(CalendarTool(config=_cal_cfg))

            if _tools_cfg.gdrive.enabled:
                from zen_claw.agent.tools.gdrive import GDriveTool

                self.tools.register(GDriveTool(config=_tools_cfg.gdrive))

            if _tools_cfg.notion.enabled:
                from zen_claw.agent.tools.notion import NotionTool

                self.tools.register(NotionTool(config=_tools_cfg.notion))
        except Exception:
            pass

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(
                CronTool(
                    self.cron_service,
                    allowed_channels=self.tool_policy_config.cron_allowed_channels,
                    allowed_actions_by_channel=self.tool_policy_config.cron_allowed_actions_by_channel,
                    require_remove_confirmation=self.tool_policy_config.cron_require_remove_confirmation,
                    max_jobs_per_session=self.tool_policy_config.max_jobs_per_session,
                )
            )

        # Agent-level policy from config; session-level rules can further narrow access.
        self.tools.set_policy_scope(
            "agent",
            allow=self.tool_policy_config.agent.allow,
            deny=self.tool_policy_config.agent.deny,
        )
        self._apply_profile_tool_binding()

        self._apply_skill_permission_gate()

    def _apply_profile_tool_binding(self) -> None:
        """Apply profile-level tool binding as an additional policy scope."""
        if self.profile_allowed_tools is None and self.profile_denied_tools is None:
            self.tools.clear_profile_tool_binding()
            return
        self.tools.set_profile_tool_binding(
            allow=self.profile_allowed_tools,
            deny=self.profile_denied_tools,
        )

    def _apply_skill_permission_gate(self) -> None:
        """Optionally restrict tools by the declared permissions of loaded skills."""
        mode = self.skill_permissions_mode
        self.tools.set_skill_attribution(self.skill_names, mode=mode)
        active_skills: set[str] = {str(n).strip() for n in self.skill_names if str(n).strip()}
        try:
            active_skills |= set(self.context.skills.get_always_skills())
        except Exception:
            pass
        untrusted_skills: list[str] = []

        if not self.skill_names or mode == "off":
            self.tools.clear_skill_allowed_tools()
            for n in active_skills:
                manifest, errors = self.context.skills.get_skill_manifest(n)
                if errors or not isinstance(manifest, dict):
                    continue
                if str((manifest or {}).get("trust") or "").strip().lower() == "untrusted":
                    untrusted_skills.append(n)
            self._apply_untrusted_skill_isolation(sorted(set(untrusted_skills)), mode=mode)
            return

        errors: list[str] = []
        allowed: set[str] = set()
        for name in active_skills:
            n = (name or "").strip()
            if not n:
                continue
            manifest, manifest_errors = self.context.skills.get_skill_manifest(n)
            if manifest_errors:
                logger.warning(
                    "Skill manifest unavailable under enforce mode; denying tool permissions for skill: "
                    + f"{n}: {'; '.join(manifest_errors)}"
                )
                untrusted_skills.append(n)
                continue
            perms = (manifest or {}).get("permissions")
            if not isinstance(perms, list) or not all(
                isinstance(p, str) and p.strip() for p in perms
            ):
                logger.warning(
                    "Skill permissions missing or invalid under enforce mode; denying tool permissions for skill: "
                    + n
                )
                untrusted_skills.append(n)
                continue
            if str((manifest or {}).get("trust") or "").strip().lower() == "untrusted":
                untrusted_skills.append(n)
            declared = {p.strip().lower() for p in perms if p.strip()}
            scopes = (manifest or {}).get("scopes")
            if isinstance(scopes, list) and all(isinstance(s, str) and s.strip() for s in scopes):
                scope_set = {s.strip().lower() for s in scopes if s.strip()}
                covered = self._permissions_from_scopes(scope_set)
                uncovered = sorted({p for p in declared if p not in covered})
                if uncovered:
                    msg = f"{n}: permissions not covered by scopes: {uncovered}"
                    if mode == "enforce":
                        errors.append(msg)
                        continue
                    logger.warning("Skill scope mismatch: " + msg)
                    declared &= covered
                if scope_set & self._HIGH_RISK_SKILL_SCOPES and mode != "enforce":
                    logger.warning(
                        "High-risk skill scopes loaded without enforce mode: "
                        + f"{n}: {sorted(scope_set & self._HIGH_RISK_SKILL_SCOPES)}"
                    )
            allowed |= declared

        if errors:
            if mode == "enforce":
                raise ValueError("skill permission gate enforce failed: " + "; ".join(errors))
            logger.warning(
                "Skill permission gate not enforced due to manifest errors: " + "; ".join(errors)
            )
            self.tools.clear_skill_allowed_tools()
            self._apply_untrusted_skill_isolation(sorted(set(untrusted_skills)), mode=mode)
            return

        self.tools.set_skill_allowed_tools(allowed)
        self._apply_untrusted_skill_isolation(sorted(set(untrusted_skills)), mode=mode)

    def _apply_untrusted_skill_isolation(
        self, untrusted_skills: list[str], mode: str = "off"
    ) -> None:
        """
        Apply hard runtime isolation constraints for untrusted skills.

        Current isolation strategy:
        - deny all local filesystem tools
        - deny privileged communication/orchestration tools
        - require sidecar/proxy backends for exec/web; otherwise deny these tools
        """
        if not untrusted_skills:
            self.tools.clear_policy_scope("skill_untrusted")
            return

        deny: set[str] = {
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
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
        }

        sidecar_ok = (
            self.exec_config.mode == "sidecar" and not self.exec_config.sidecar_fallback_to_local
        )
        search_proxy_ok = (
            self.web_search_config.mode == "proxy"
            and not self.web_search_config.proxy_fallback_to_local
        )
        fetch_proxy_ok = (
            self.web_fetch_config.mode == "proxy"
            and not self.web_fetch_config.proxy_fallback_to_local
        )

        if not sidecar_ok:
            deny.add("exec")
        if not search_proxy_ok:
            deny.add("web_search")
        if not fetch_proxy_ok:
            deny.add("web_fetch")

        problems: list[str] = []
        if not sidecar_ok:
            problems.append("exec requires sidecar mode with fallback disabled")
        if not search_proxy_ok:
            problems.append("web_search requires proxy mode with fallback disabled")
        if not fetch_proxy_ok:
            problems.append("web_fetch requires proxy mode with fallback disabled")
        if problems and mode == "enforce":
            joined = "; ".join(problems)
            raise ValueError(f"untrusted skill isolation requirements not met: {joined}")

        self.tools.set_policy_scope("skill_untrusted", deny=sorted(deny))
        logger.warning(
            "Applied untrusted skill isolation constraints "
            + TraceContext.event_text(
                "skill.untrusted.isolation",
                None,
                policy_scope="skill_untrusted",
                policy_code="untrusted_skill_isolation",
                error_kind="permission",
                retryable=False,
                skill_names=untrusted_skills,
                message="runtime isolation constraints active",
            )
        )

    def _permissions_from_scopes(self, scopes: set[str]) -> set[str]:
        scope_to_permissions = {
            "network": {"web_search", "web_fetch"},
            "filesystem": {"read_file", "write_file", "edit_file", "list_dir"},
            "exec": {"exec", "spawn", "gateway"},
            "message": {"message"},
            "cron": {"cron"},
            "sessions": {
                "sessions_spawn",
                "sessions_list",
                "sessions_kill",
                "sessions_read",
                "sessions_write",
                "sessions_signal",
                "sessions_resize",
            },
        }
        out: set[str] = set()
        for s in scopes:
            out |= scope_to_permissions.get(s, set())
        return out

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        try:
            self.context.skills.start_gc_reaper()
        except Exception as exc:
            logger.warning(f"Failed to start skill GC reaper: {exc}")

        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)

                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    trace_id = msg.trace_id or TraceContext.new_trace_id()
                    logger.error(
                        f"Error processing message: {e} "
                        + TraceContext.event_text(
                            "agent.process.error",
                            trace_id,
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            error_kind="runtime",
                            retryable=False,
                        )
                    )
                    # Send error response
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"Sorry, I encountered an error: {str(e)}",
                            metadata=TraceContext.child_metadata(trace_id),
                        )
                    )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        try:
            self.context.skills.stop_gc_reaper()
        except Exception as exc:
            logger.warning(f"Failed to stop skill GC reaper: {exc}")
        logger.info("Agent loop stopping")

    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.

        Returns:
            The response message, or None if no response needed.
        """
        trace_id, msg.metadata = TraceContext.ensure_trace_id(msg.metadata)

        if self._audit_worker:
            await self._audit_worker.audit_turn(
                trace_id,
                {
                    "event_type": "message.inbound",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "sender_id": getattr(msg, "sender_id", None),
                },
            )

        # Resolve session early so runtime commands can be session-local.
        session = self.sessions.get_or_create(msg.session_key)

        # ── Handle /approve and /deny commands ────────────────────────────────
        stripped = msg.content.strip()
        if self.approval_gate and stripped.startswith(("/approve ", "/deny ")):
            parts = stripped.split(None, 2)
            decision = parts[0].lstrip("/").lower()  # "approve" or "deny"
            aid = parts[1].upper() if len(parts) > 1 else ""
            if decision == "approve":
                rec = self.approval_gate.approve(aid)
            else:
                rec = self.approval_gate.deny(aid)

            if rec is None:
                reply = f"⚠️ 未找到审批请求 `{aid}`，或已处理完毕。"
            elif rec.status == ApprovalStatus.APPROVED:
                reply = f"✅ 已授权 `{rec.tool_name}` 操作（审批 ID: `{aid}`），Agent 下次运行时将继续执行。"
            else:
                reply = f"🚫 已拒绝 `{rec.tool_name}` 操作（审批 ID: `{aid}`），该调用已取消。"

            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=reply,
                metadata=TraceContext.child_metadata(trace_id),
            )
        # ── End command intercept ─────────────────────────────────────────────

        runtime_reply = self._handle_runtime_command(stripped, session)
        if runtime_reply is not None:
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=runtime_reply,
                metadata=TraceContext.child_metadata(trace_id),
            )

        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        if msg.channel == "cli" and not (msg.metadata or {}).get("channel_role"):
            msg.metadata["channel_role"] = "admin"
        elif msg.metadata is not None and not msg.metadata.get("channel_role"):
            msg.metadata["channel_role"] = "user"
        if msg.metadata is not None:
            msg.metadata.setdefault("sender_id", msg.sender_id)
            msg.metadata.setdefault("workspace_id", self.workspace.name)
            msg.metadata.setdefault("agent_profile", self.agent_id)
            self._hydrate_identity_metadata(msg.metadata)

        deny_reason = self._fail_closed_identity_reason(msg.channel, msg.metadata)
        if deny_reason:
            logger.warning(
                f"Identity fail-closed denied request: {deny_reason} "
                + TraceContext.event_text(
                    "agent.identity.denied",
                    trace_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    sender_id=msg.sender_id,
                    error_kind="permission",
                    retryable=False,
                )
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Access Denied: {deny_reason}",
                metadata=TraceContext.child_metadata(trace_id),
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(
            f"Processing message from {msg.channel}:{msg.sender_id}: {preview} "
            + TraceContext.event_text(
                "agent.inbound",
                trace_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
            )
        )

        self._apply_channel_tool_policy(msg.channel)
        if self._should_enforce_identity(msg.metadata):
            self._apply_channel_role_tool_policy(msg.metadata)
        else:
            self.tools.clear_policy_scope("channel_role")
        self._apply_session_tool_policy(session.metadata)
        self.tools.set_request_context(
            self._build_request_security_context(
                channel=msg.channel,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                metadata=msg.metadata,
                trace_id=trace_id,
                session_metadata=session.metadata,
            )
        )

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id, trace_id=trace_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(
                msg.channel,
                msg.chat_id,
                trace_id=trace_id,
                skill_pins=session.metadata.get("skill_pins"),
            )

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        # Populate skill pins for session-level version consistency
        if "skill_pins" not in session.metadata:
            all_active = list(self.skill_names)
            all_active.extend(self.context.skills.get_always_skills())
            session.metadata["skill_pins"] = self.context.skills.build_session_pins(all_active)

        self.intent_router.set_control_context(session.metadata)
        try:
            route_result = await self.intent_router.route(
                msg.content,
                tools=self.tools,
                trace_id=trace_id,
            )
        finally:
            self.intent_router.set_control_context(None)
        gate2_instruction: str | None = None
        clarification_reply: str | None = None
        selected_skill: str | None = None
        if route_result.safety_valve_outcome == "delegate":
            route_result, gate2_instruction, clarification_reply, selected_skill = await self._run_gate2_classifier(
                msg=msg,
                session=session,
                route_result=route_result,
                trace_id=trace_id,
            )

        self._append_intent_router_event(trace_id=trace_id, msg=msg, route_result=route_result)
        self._update_intent_router_control_state(session.metadata, route_result)
        execution_intent = build_execution_intent(
            route_result,
            clarification_question=clarification_reply,
            selected_skill=selected_skill,
        )
        route_result, execution_intent, explicit_approved_tools, immediate_response = (
            await self._resolve_immediate_execution_intent(
                msg=msg,
                session=session,
                trace_id=trace_id,
                route_result=route_result,
                execution_intent=execution_intent,
                clarification_reply=clarification_reply,
                selected_skill=selected_skill,
            )
        )
        if immediate_response is not None:
            return immediate_response

        history = await self._build_history_with_compression(session, trace_id)

        # Build initial messages (use compressed history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=self.skill_names,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            pins=session.metadata["skill_pins"],
        )
        constrained_tools: list[dict[str, Any]] | None = None
        if route_result.route_status == "needs_constrained_replan" and route_result.contract:
            messages.append(
                {
                    "role": "system",
                    "content": self._build_intent_replan_instruction(route_result),
                }
            )
            self._apply_intent_contract_policy(route_result.contract)
            constrained_tools = self.tools.get_visible_definitions(
                extra_allow=route_result.contract.allowed_tools,
                extra_deny=route_result.contract.denied_tools,
            )
        if gate2_instruction:
            messages.append({"role": "system", "content": gate2_instruction})
        if bool(session.metadata.get("think_enabled", False)):
            messages.append(
                {
                    "role": "system",
                    "content": "Apply deeper reasoning and double-check key assumptions before final answers.",
                }
            )

        session_override_model = str(session.metadata.get("override_model") or "").strip()
        preferred_model = session_override_model or self.model
        allow_model_fallback = not bool(session_override_model)
        routing_metadata = {**dict(session.metadata or {}), **dict(msg.metadata or {})}
        run_model_reason = self._resolve_run_model_reason(
            messages,
            intent_name=route_result.intent_name,
            think_enabled=bool(session.metadata.get("think_enabled", False)),
            allow_dynamic_override=allow_model_fallback,
            metadata=routing_metadata,
        )
        run_model = self._resolve_run_model(
            messages,
            preferred_model=preferred_model,
            intent_name=route_result.intent_name,
            think_enabled=bool(session.metadata.get("think_enabled", False)),
            allow_dynamic_override=allow_model_fallback,
            metadata=routing_metadata,
        )
        self._append_model_selection_event(
            trace_id=trace_id,
            msg=msg,
            selected_model=run_model,
            reason=run_model_reason,
            intent_name=route_result.intent_name or "",
        )
        self._last_run_model_used = run_model
        self._last_run_model_reason = run_model_reason
        try:
            final_content, usage, _, route_result = await self._dispatch_execution_intent(
                msg=msg,
                session=session,
                trace_id=trace_id,
                route_result=route_result,
                execution_intent=execution_intent,
                messages=messages,
                run_model=run_model,
                run_model_reason=run_model_reason,
                explicit_approved_tools=explicit_approved_tools,
                constrained_tools=constrained_tools,
                allow_model_fallback=allow_model_fallback,
            )
        finally:
            self.tools.clear_policy_scope("intent_contract")

        if (
            route_result.route_status == "needs_constrained_replan"
            and route_result.contract is not None
            and not route_result.contract.allow_high_risk_escalation
            and self._looks_like_permission_escalation_text(final_content)
        ):
            final_content = self._build_non_permission_route_failure(route_result)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Log response preview
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(
            f"Response to {msg.channel}:{msg.sender_id}: {preview} "
            + TraceContext.event_text(
                "agent.outbound.ready",
                trace_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
        )

        if usage:
            session.metadata["last_usage"] = usage
        session.metadata["last_model"] = str(self._last_run_model_used or run_model)
        if (
            self._last_run_model_used
            and self._last_run_model_used != run_model
            and self._last_run_model_reason
        ):
            self._append_model_selection_event(
                trace_id=trace_id,
                msg=msg,
                selected_model=self._last_run_model_used,
                reason=self._last_run_model_reason,
                intent_name=route_result.intent_name or "",
            )
        if bool(session.metadata.get("verbose", False)) and usage:
            usage_text = ", ".join(f"{k}={v}" for k, v in sorted(usage.items()))
            final_content += (
                f"\n\n[verbose] model={str(self._last_run_model_used or run_model)}; usage: {usage_text}"
            )

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        await self._extract_and_store_memory(msg.content, final_content, trace_id)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=TraceContext.child_metadata(trace_id),
        )

    async def _resolve_immediate_execution_intent(
        self,
        *,
        msg: InboundMessage,
        session: "Session",
        trace_id: str,
        route_result: IntentRouteResult,
        execution_intent: Any,
        clarification_reply: str | None,
        selected_skill: str | None,
    ) -> tuple[IntentRouteResult, Any, set[str], OutboundMessage | None]:
        if execution_intent.path_type == "clarification" and clarification_reply is not None:
            self._append_execution_event(
                trace_id=trace_id,
                msg=msg,
                route_result=route_result,
                execution_intent=execution_intent,
                execution_result_status="clarified",
                final_content=clarification_reply,
                selected_model="",
                model_reason="",
            )
            session.add_message("user", msg.content)
            session.add_message("assistant", clarification_reply)
            self.sessions.save(session)
            return (
                route_result,
                execution_intent,
                set(),
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=clarification_reply,
                    metadata=TraceContext.child_metadata(trace_id),
                ),
            )

        explicit_approved_tools: set[str] = set()
        if execution_intent.path_type == "approval_wait":
            explicit_approved_tools, approval_reply = await self._resolve_one_shot_explicit_approval(
                msg,
                session,
                route_result,
                trace_id,
            )
            if approval_reply is not None:
                self._append_execution_event(
                    trace_id=trace_id,
                    msg=msg,
                    route_result=route_result,
                    execution_intent=execution_intent,
                    execution_result_status="approval_required",
                    final_content=approval_reply,
                    failure_classification="approval_wait",
                    selected_model="",
                    model_reason="",
                )
                return (
                    route_result,
                    execution_intent,
                    explicit_approved_tools,
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=approval_reply,
                        metadata=TraceContext.child_metadata(trace_id),
                    ),
                )
            if explicit_approved_tools and route_result.contract is not None:
                route_result = IntentRouteResult(
                    handled=route_result.handled,
                    intent_name=route_result.intent_name,
                    content=route_result.content,
                    contract=self._contract_with_one_shot_approval(
                        route_result.contract,
                        explicit_approved_tools,
                    ),
                    route_status="needs_constrained_replan",
                    diagnostic=route_result.diagnostic,
                    skip_planning=route_result.skip_planning,
                    recovery_outcome=route_result.recovery_outcome,
                    route_candidate=route_result.route_candidate,
                    delegate_reason=route_result.delegate_reason,
                    safety_valve_outcome=route_result.safety_valve_outcome,
                    arbitration_result=route_result.arbitration_result,
                    safety_valve_trace=route_result.safety_valve_trace,
                )
                self._append_intent_router_event(
                    trace_id=trace_id,
                    msg=msg,
                    route_result=route_result,
                )
                execution_intent = build_execution_intent(
                    route_result,
                    clarification_question=clarification_reply,
                    selected_skill=selected_skill,
                )

        if execution_intent.path_type == "direct" and route_result.content is not None:
            direct_content = route_result.content
            if self._should_schedule_deferred_retry(msg, route_result):
                self._schedule_deferred_retry(msg, route_result, trace_id)
                direct_content += "\n\n我会在后台再试一次；若成功会把结果回推到当前会话。"
            self._append_execution_event(
                trace_id=trace_id,
                msg=msg,
                route_result=route_result,
                execution_intent=execution_intent,
                execution_result_status=(
                    "succeeded" if route_result.route_status == "direct_success" else "failed"
                ),
                final_content=direct_content,
                failure_classification=(
                    None
                    if route_result.route_status == "direct_success"
                    else str(route_result.diagnostic or "").strip() or "direct_failed"
                ),
                selected_model="",
                model_reason="",
            )
            session.add_message("user", msg.content)
            session.add_message("assistant", direct_content)
            await self._extract_and_store_memory(msg.content, direct_content, trace_id)
            self.sessions.save(session)
            return (
                route_result,
                execution_intent,
                explicit_approved_tools,
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=direct_content,
                    metadata=TraceContext.child_metadata(trace_id),
                ),
            )

        return route_result, execution_intent, explicit_approved_tools, None

    async def _dispatch_execution_intent(
        self,
        *,
        msg: InboundMessage,
        session: "Session",
        trace_id: str,
        route_result: IntentRouteResult,
        execution_intent: Any,
        messages: list[dict[str, Any]],
        run_model: str,
        run_model_reason: str,
        explicit_approved_tools: set[str],
        constrained_tools: list[dict[str, Any]] | None,
        allow_model_fallback: bool,
    ) -> tuple[str | None, dict[str, int], str | None, IntentRouteResult]:
        if execution_intent.path_type == "gate3_plan":
            route_result.diagnostic = (
                f"{route_result.diagnostic}:gate3_entry"
                if route_result.diagnostic
                else "gate3:entry"
            )
            final_content, usage = await self._enter_gate3_agent_loop(
                messages=messages,
                msg=msg,
                session=session,
                trace_id=trace_id,
                route_result=route_result,
                run_model=run_model,
                explicit_approved_tools=explicit_approved_tools,
                constrained_tools=constrained_tools,
                allow_model_fallback=allow_model_fallback,
            )
            self._append_intent_router_event(
                trace_id=trace_id,
                msg=msg,
                route_result=route_result,
            )
            failure_classification = None if final_content else "empty_gate3_result"
            self._append_execution_event(
                trace_id=trace_id,
                msg=msg,
                route_result=route_result,
                execution_intent=execution_intent,
                execution_result_status="succeeded" if final_content else "failed",
                final_content=final_content,
                failure_classification=failure_classification,
                selected_model=run_model,
                model_reason=run_model_reason,
                trace_summary=self._last_execution_trace_summary,
            )
            return final_content, usage, failure_classification, route_result

        if not route_result.skip_planning:
            messages = await self._run_plan_phase(messages, msg.content, trace_id, model=run_model)
        usage: dict[str, int] = {}
        final_content, _ = await self._run_execute_reflect_loop(
            messages,
            trace_id,
            session=session,
            model=run_model,
            channel=msg.channel,
            chat_id=msg.chat_id,
            usage_collector=usage,
            tool_definitions=constrained_tools,
            approved_one_shot_tools=explicit_approved_tools,
            active_intent_contract=route_result.contract,
            allow_model_fallback=allow_model_fallback,
        )
        failure_classification = None if final_content else "empty_execution_result"
        self._append_execution_event(
            trace_id=trace_id,
            msg=msg,
            route_result=route_result,
            execution_intent=execution_intent,
            execution_result_status="succeeded" if final_content else "failed",
            final_content=final_content,
            failure_classification=failure_classification,
            selected_model=run_model,
            model_reason=run_model_reason,
            trace_summary=self._last_execution_trace_summary,
        )
        return final_content, usage, failure_classification, route_result

    def _handle_runtime_command(self, stripped: str, session: Any) -> str | None:
        """Handle runtime slash commands scoped to the current session."""
        if not stripped.startswith("/"):
            return None

        if stripped.startswith("/model-profile"):
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                current = str(session.metadata.get("model_profile") or "auto").strip().lower() or "auto"
                return f"当前会话模型策略：`{current}`"
            mode = parts[1].strip().lower()
            if mode not in {"auto", "cheap", "stable"}:
                return "用法：`/model-profile cheap`、`/model-profile stable` 或 `/model-profile auto`"
            if mode == "auto":
                session.metadata.pop("model_profile", None)
                return "🧭 当前会话模型策略已重置为 `auto`。"
            session.metadata["model_profile"] = mode
            return f"🧭 当前会话模型策略已设为 `{mode}`。"

        if stripped.startswith("/model"):
            parts = stripped.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                current = str(session.metadata.get("override_model") or self.model)
                return f"当前会话模型：`{current}`"
            new_model = parts[1].strip()
            if self.allowed_models and new_model.lower() not in self.allowed_models:
                return f"🚫 切换失败：模型 `{new_model}` 不在允许白名单中。"
            old_model = str(session.metadata.get("override_model") or self.model)
            session.metadata["override_model"] = new_model
            return f"🔄 当前会话模型已从 `{old_model}` 切换为 `{new_model}`。"

        if stripped == "/clear":
            session.clear()
            session.metadata.pop("rolling_summary", None)
            session.metadata.pop("rolling_summary_upto", None)
            return "🧹 当前会话上下文已清空。"

        if stripped.startswith("/think"):
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                enabled = bool(session.metadata.get("think_enabled", False))
                return f"当前会话推理增强：`{'on' if enabled else 'off'}`"
            mode = parts[1].strip().lower()
            if mode not in {"on", "off"}:
                return "用法：`/think on` 或 `/think off`"
            session.metadata["think_enabled"] = mode == "on"
            return f"🧠 当前会话推理增强已设为 `{mode}`。"

        if stripped.startswith("/verbose"):
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                enabled = bool(session.metadata.get("verbose", False))
                return f"当前会话详细输出：`{'on' if enabled else 'off'}`"
            mode = parts[1].strip().lower()
            if mode not in {"on", "off"}:
                return "用法：`/verbose on` 或 `/verbose off`"
            session.metadata["verbose"] = mode == "on"
            return f"🛠️ 当前会话详细输出已设为 `{mode}`。"

        if stripped == "/usage":
            usage = session.metadata.get("last_usage")
            model = str(
                session.metadata.get("last_model")
                or session.metadata.get("override_model")
                or self.model
            )
            if not isinstance(usage, dict) or not usage:
                return f"📊 暂无用量数据（model=`{model}`）"
            usage_text = ", ".join(f"{k}={v}" for k, v in sorted(usage.items()))
            return f"📊 最近一次用量（model=`{model}`）：{usage_text}"

        return None

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        trace_id, msg.metadata = TraceContext.ensure_trace_id(msg.metadata)
        logger.info(
            f"Processing system message from {msg.sender_id} "
            + TraceContext.event_text(
                "agent.system.inbound",
                trace_id,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
            )
        )

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        self._apply_channel_tool_policy(origin_channel)
        self._apply_channel_role_tool_policy({"channel_role": "admin"})
        self._apply_session_tool_policy(session.metadata)
        self.tools.set_request_context(
            self._build_request_security_context(
                channel=origin_channel,
                sender_id=msg.sender_id,
                chat_id=origin_chat_id,
                metadata={"channel_role": "admin"},
                trace_id=trace_id,
                session_metadata=session.metadata,
            )
        )

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id, trace_id=trace_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id, trace_id=trace_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        history = await self._build_history_with_compression(session, trace_id)

        # Build messages with the announce content
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=self.skill_names,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        if bool(session.metadata.get("think_enabled", False)):
            messages.append(
                {
                    "role": "system",
                    "content": "Apply deeper reasoning and double-check key assumptions before final answers.",
                }
            )

        session_override_model = str(session.metadata.get("override_model") or "").strip()
        preferred_model = session_override_model or self.model
        allow_model_fallback = not bool(session_override_model)
        routing_metadata = {**dict(session.metadata or {}), **dict(msg.metadata or {})}
        run_model_reason = self._resolve_run_model_reason(
            messages,
            think_enabled=bool(session.metadata.get("think_enabled", False)),
            allow_dynamic_override=allow_model_fallback,
            metadata=routing_metadata,
        )
        run_model = self._resolve_run_model(
            messages,
            preferred_model=preferred_model,
            think_enabled=bool(session.metadata.get("think_enabled", False)),
            allow_dynamic_override=allow_model_fallback,
            metadata=routing_metadata,
        )
        self._append_model_selection_event(
            trace_id=trace_id,
            msg=msg,
            selected_model=run_model,
            reason=run_model_reason,
        )
        self._last_run_model_used = run_model
        self._last_run_model_reason = run_model_reason
        messages = await self._run_plan_phase(messages, msg.content, trace_id, model=run_model)
        final_content, _ = await self._run_execute_reflect_loop(
            messages,
            trace_id,
            session=session,
            model=run_model,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        if final_content is None:
            final_content = "Background task completed."

        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        await self._extract_and_store_memory(msg.content, final_content, trace_id)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content,
            metadata=TraceContext.child_metadata(trace_id),
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                **TraceContext.child_metadata(None),
                "session_key": session_key,
                "channel_role": "admin",
                "sender_id": "user",
                "tenant_id": "local",
                "role": "operator",
                "workspace_id": self.workspace.name,
                "agent_profile": self.agent_id,
            },
        )

        response = await self._process_message(msg)
        return response.content if response else ""

    def _append_intent_router_event(
        self,
        *,
        trace_id: str,
        msg: InboundMessage,
        route_result: IntentRouteResult,
    ) -> None:
        try:
            from zen_claw.config.loader import get_data_dir

            dashboard_dir = get_data_dir() / "dashboard"
            dashboard_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "at_ms": int(datetime.now(UTC).timestamp() * 1000),
                "trace_id": trace_id,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "intent_name": route_result.intent_name or "",
                "route_status": route_result.route_status,
                "handled": bool(route_result.handled),
                "diagnostic": str(route_result.diagnostic or ""),
                "recovery_mode": (
                    str(route_result.recovery_outcome.mode)
                    if route_result.recovery_outcome is not None
                    else ""
                ),
                "recovery_blocker_kind": (
                    str(route_result.recovery_outcome.plan.blocker.kind)
                    if route_result.recovery_outcome is not None
                    and route_result.recovery_outcome.plan is not None
                    else ""
                ),
                "delegate_reason": str(route_result.delegate_reason or ""),
                "safety_valve_outcome": str(route_result.safety_valve_outcome or ""),
                "arbitration_result": str(route_result.arbitration_result or ""),
                "route_candidate_match": "",
                "route_candidate_source": "",
                "route_candidate_raw_confidence": "",
                "route_candidate_crystallized_route": "",
                "safety_valve_weighted_confidence": "",
                "safety_valve_final_confidence": "",
                "safety_valve_residual_ratio": "",
                "safety_valve_matched_candidates_count": "",
                "safety_valve_has_context_pronoun": False,
                "safety_valve_length_outlier": False,
                "routing_stage": "",
                "entered_gate3": False,
            }
            routing_stage, entered_gate3 = self._resolve_intent_router_stage(route_result)
            event["routing_stage"] = routing_stage
            event["entered_gate3"] = entered_gate3
            candidate = route_result.route_candidate if isinstance(route_result.route_candidate, dict) else None
            if candidate is not None:
                event["route_candidate_match"] = str(candidate.get("match") or "")
                event["route_candidate_source"] = str(candidate.get("source") or "")
                event["route_candidate_crystallized_route"] = str(
                    candidate.get("crystallized_route") or ""
                )
                raw_confidence = candidate.get("raw_confidence")
                if raw_confidence is not None:
                    event["route_candidate_raw_confidence"] = raw_confidence
            trace = route_result.safety_valve_trace if isinstance(route_result.safety_valve_trace, dict) else None
            if trace is not None:
                event["safety_valve_weighted_confidence"] = trace.get("weighted_confidence", "")
                event["safety_valve_final_confidence"] = trace.get("final_confidence", "")
                event["safety_valve_residual_ratio"] = trace.get("residual_ratio", "")
                event["safety_valve_matched_candidates_count"] = trace.get(
                    "matched_candidates_count", ""
                )
                event["safety_valve_has_context_pronoun"] = bool(trace.get("has_context_pronoun"))
                event["safety_valve_length_outlier"] = bool(trace.get("length_outlier"))
            with (dashboard_dir / "intent_router.log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            return

    def _append_execution_event(
        self,
        *,
        trace_id: str,
        msg: InboundMessage,
        route_result: IntentRouteResult,
        execution_intent: Any,
        execution_result_status: str,
        final_content: str | None,
        selected_model: str,
        model_reason: str,
        failure_classification: str | None = None,
        trace_summary: dict[str, Any] | None = None,
        ) -> None:
        try:
            from zen_claw.config.loader import get_data_dir

            dashboard_dir = get_data_dir() / "dashboard"
            dashboard_dir.mkdir(parents=True, exist_ok=True)
            route_candidate = RouteCandidate.from_payload(route_result.route_candidate)
            routing_decision = build_routing_decision(route_result)
            arbitration_decision = build_arbitration_decision(route_result)
            normalized_failure = self._normalize_execution_failure_classification(
                route_result=route_result,
                execution_intent=execution_intent,
                execution_result_status=execution_result_status,
                failure_classification=failure_classification,
                trace_summary=trace_summary,
            )
            preview = str(final_content or "").strip()
            if len(preview) > 160:
                preview = preview[:157] + "..."
            merged_trace_summary = {
                "selected_model": str(selected_model or ""),
                "model_reason": str(model_reason or ""),
                **dict(trace_summary or {}),
            }
            raw_failure = str(failure_classification or "").strip()
            if raw_failure and raw_failure != normalized_failure:
                merged_trace_summary["failure_detail"] = raw_failure
            event = {
                "at_ms": int(datetime.now(UTC).timestamp() * 1000),
                "trace_id": trace_id,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "intent_name": route_result.intent_name or "",
                "route_candidate": route_candidate.as_dict() if route_candidate is not None else None,
                "routing_decision": routing_decision.as_dict(),
                "arbitration_decision": arbitration_decision.as_dict(),
                "execution_intent": execution_intent.as_dict(),
                "execution_result": {
                    "status": str(execution_result_status or ""),
                    "failure_classification": normalized_failure,
                    "final_content_preview": preview,
                    "trace_summary": merged_trace_summary,
                },
            }
            with (dashboard_dir / "agent_execution.log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            return

    @staticmethod
    def _normalize_execution_failure_classification(
        *,
        route_result: IntentRouteResult,
        execution_intent: Any,
        execution_result_status: str,
        failure_classification: str | None,
        trace_summary: dict[str, Any] | None,
    ) -> str:
        status = str(execution_result_status or "").strip().lower()
        raw = str(failure_classification or "").strip().lower()
        trace = dict(trace_summary or {})
        last_error_kind = str(trace.get("last_error_kind") or "").strip().lower()
        last_error_code = str(trace.get("last_error_code") or "").strip().lower()

        if not raw and status != "failed":
            return raw
        if raw == "approval_wait" or status == "approval_required" or last_error_code == "approval_required":
            return "approval_wait"
        if raw in {"empty_execution_result", "empty_gate3_result"}:
            return "no_result"
        if raw == "tool_timeout" or "timeout" in last_error_code:
            return "tool_timeout"
        if raw in {"intent_contract_denied", "contract_denied"} or last_error_code in {
            "intent_contract_denied",
            "contract_denied",
        }:
            return "permission_denied"
        if raw == "direct_failed" or str(route_result.route_status or "").strip().lower() == "direct_failed":
            return "direct_route_failure"
        if raw == "retryable" or last_error_kind == "retryable":
            return "tool_retryable"
        if raw in {"tool_runtime", "runtime"} or last_error_kind == "runtime":
            return "tool_runtime"
        if raw in {"tool_permission", "permission"} or last_error_kind == "permission":
            return "tool_permission"
        if raw:
            return raw
        if status == "failed":
            if execution_intent is not None and getattr(execution_intent, "path_type", "") == "approval_wait":
                return "approval_wait"
            return "execution_failed"
        return ""

    @staticmethod
    def _update_intent_router_control_state(
        metadata: dict[str, Any],
        route_result: IntentRouteResult,
    ) -> None:
        candidate = route_result.route_candidate if isinstance(route_result.route_candidate, dict) else None
        source = str(candidate.get("source") or "").strip().lower() if candidate else ""
        intent_name = str(route_result.intent_name or "").strip()
        crystallized_name = str(candidate.get("crystallized_route") or "").strip() if candidate else ""

        if route_result.route_status in {"direct_success", "direct_failed"} and source:
            history = metadata.get("intent_router_history")
            if not isinstance(history, dict):
                history = {}
                metadata["intent_router_history"] = history
            intent_history = history.get(intent_name)
            if not isinstance(intent_history, dict):
                intent_history = {"success": 0, "failure": 0}
                history[intent_name] = intent_history
            success = max(int(intent_history.get("success", 0) or 0), 0)
            failure = max(int(intent_history.get("failure", 0) or 0), 0)
            if route_result.route_status == "direct_success":
                success += 1
            else:
                failure += 1
            intent_history["success"] = success
            intent_history["failure"] = failure
            metadata["intent_router_last_rule"] = {
                "intent_name": intent_name,
                "source": source,
                "route_status": route_result.route_status,
            }
            if source == "crystallized" and crystallized_name:
                state_map = metadata.get("intent_router_crystallized_state")
                if not isinstance(state_map, dict):
                    state_map = {}
                    metadata["intent_router_crystallized_state"] = state_map
                route_state = state_map.get(crystallized_name)
                if not isinstance(route_state, dict):
                    route_state = {"success": 0, "failure": 0, "consecutive_failures": 0}
                    state_map[crystallized_name] = route_state
                route_success = max(int(route_state.get("success", 0) or 0), 0)
                route_failure = max(int(route_state.get("failure", 0) or 0), 0)
                if route_result.route_status == "direct_success":
                    route_success += 1
                    route_state["consecutive_failures"] = 0
                else:
                    route_failure += 1
                    route_state["consecutive_failures"] = max(
                        int(route_state.get("consecutive_failures", 0) or 0),
                        0,
                    ) + 1
                route_state["success"] = route_success
                route_state["failure"] = route_failure
            return

        metadata.pop("intent_router_last_rule", None)

    @staticmethod
    def _resolve_intent_router_stage(route_result: IntentRouteResult) -> tuple[str, bool]:
        return resolve_routing_stage(route_result)

    async def _run_gate2_classifier(
        self,
        *,
        msg: InboundMessage,
        session: "Session",
        route_result: IntentRouteResult,
        trace_id: str,
    ) -> tuple[IntentRouteResult, str | None, str | None, str | None]:
        history = session.get_history(max_messages=6)
        session_override_model = str(session.metadata.get("override_model") or "").strip()
        classifier_model = session_override_model or self.model
        arbitration = await self.intent_router_classifier.classify(
            provider=self.provider,
            model=classifier_model,
            content=msg.content,
            history=history,
            route_candidate=route_result.route_candidate,
            delegate_reason=route_result.delegate_reason,
            available_skills=self.skill_names,
        )
        route_result.arbitration_result = arbitration.kind

        if arbitration.kind == "confirm_candidate":
            return route_result, None, None, None

        if arbitration.kind == "select_skill":
            skill_name = str(arbitration.skill_name or "").strip()
            instruction = (
                f"Gate 2 selected skill `{skill_name}`. Prefer that skill if it is relevant and available. "
                "Do not ignore the delegated routing hint."
            )
            return route_result, instruction, None, skill_name

        if arbitration.kind == "request_clarification":
            question = str(arbitration.clarification_question or "").strip()
            if not question:
                question = "我需要一点澄清：你具体是想让我做哪一种？"
            return route_result, None, question, None

        route_result.contract = None
        route_result.skip_planning = False
        route_result.diagnostic = (
            f"{route_result.diagnostic}:gate2_unclassified"
            if route_result.diagnostic
            else "gate2:unclassified"
        )
        route_result.contract = self._GATE3_DEFAULT_CONTRACT
        return route_result, None, None, None

    async def _enter_gate3_agent_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        msg: InboundMessage,
        session: "Session",
        trace_id: str,
        route_result: IntentRouteResult,
        run_model: str,
        explicit_approved_tools: set[str],
        constrained_tools: list[dict[str, Any]] | None,
        allow_model_fallback: bool,
    ) -> tuple[str | None, dict[str, int]]:
        gate3_messages = list(messages)
        usage: dict[str, int] = {}
        if not route_result.skip_planning:
            gate3_messages = await self._run_plan_phase(
                gate3_messages,
                msg.content,
                trace_id,
                model=run_model,
            )
        final_content, _ = await self._run_execute_reflect_loop(
            gate3_messages,
            trace_id,
            session=session,
            model=run_model,
            channel=msg.channel,
            chat_id=msg.chat_id,
            usage_collector=usage,
            tool_definitions=constrained_tools,
            approved_one_shot_tools=explicit_approved_tools,
            active_intent_contract=route_result.contract,
            allow_model_fallback=allow_model_fallback,
        )
        return final_content, usage

    def _append_model_selection_event(
        self,
        *,
        trace_id: str,
        msg: InboundMessage,
        selected_model: str,
        reason: str,
        intent_name: str = "",
    ) -> None:
        try:
            from zen_claw.config.loader import get_data_dir

            dashboard_dir = get_data_dir() / "dashboard"
            dashboard_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "at_ms": int(datetime.now(UTC).timestamp() * 1000),
                "trace_id": trace_id,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "intent_name": intent_name,
                "selected_model": selected_model,
                "reason": reason,
            }
            with (dashboard_dir / "model_routing.log.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            return

    async def _resolve_one_shot_explicit_approval(
        self,
        msg: InboundMessage,
        session: "Session",
        route_result: IntentRouteResult,
        trace_id: str,
    ) -> tuple[set[str], str | None]:
        approval_args = self._build_one_shot_approval_args(route_result)
        if approval_args is None:
            return set(), "当前高风险升级请求缺少最小授权范围信息，因此不能发起一次性授权。"
        if self.approval_gate is None:
            return set(), "当前运行环境未启用审批网关，因此不能处理一次性高风险授权。"

        approved = self.approval_gate.consume_approved(
            session.key,
            "intent_one_shot_approval",
            approval_args,
        )
        if approved is not None:
            requested_tools = approval_args.get("approved_tools")
            if isinstance(requested_tools, list):
                return {str(tool).strip().lower() for tool in requested_tools if str(tool).strip()}, None
            return set(), None

        approval = await self.approval_gate.request_approval(
            session_id=session.key,
            tool_name="intent_one_shot_approval",
            tool_args=approval_args,
            reason=self._build_one_shot_approval_reason(route_result, approval_args),
            bus=self.bus,
            channel=msg.channel,
            chat_id=msg.chat_id,
            trace_id=trace_id,
            capability="intent.one_shot_approval",
            resource_scope={"approved_tools": list(approval_args.get("approved_tools") or [])},
            security_context=self._build_request_security_context(
                channel=msg.channel,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                metadata=msg.metadata,
                trace_id=trace_id,
                session_metadata=session.metadata if session is not None else None,
            ),
            policy_snapshot_hash=str(
                (
                    self._build_request_security_context(
                        channel=msg.channel,
                        sender_id=msg.sender_id,
                        chat_id=msg.chat_id,
                        metadata=msg.metadata,
                        trace_id=trace_id,
                        session_metadata=session.metadata if session is not None else None,
                    ).get("policy_snapshot_hash")
                )
                or ""
            ),
            decision_basis=self._build_one_shot_approval_reason(route_result, approval_args),
        )
        approval_msg = approval.format_request_message()
        if msg.channel == "cli":
            if route_result.content:
                return set(), f"{route_result.content}\n\n{approval_msg}"
            return set(), approval_msg
        return set(), (
            f"{route_result.content or '当前请求需要一次性显式授权。'}\n\n"
            f"{approval_msg}"
        )

    def _build_one_shot_approval_args(
        self,
        route_result: IntentRouteResult,
    ) -> dict[str, Any] | None:
        contract = route_result.contract
        if contract is None or not contract.allow_high_risk_escalation:
            return None
        requested_tools = self._parse_one_shot_requested_tools(route_result)
        if not requested_tools:
            return None
        return {
            "intent": route_result.intent_name or contract.intent_name,
            "approved_tools": sorted(requested_tools),
        }

    def _parse_one_shot_requested_tools(self, route_result: IntentRouteResult) -> set[str]:
        diagnostic = str(route_result.diagnostic or "")
        prefix = "explicit_approval:"
        if not diagnostic.startswith(prefix):
            return set()
        requested_tools: set[str] = set()
        for token in diagnostic[len(prefix) :].split(","):
            tool_name = token.strip().lower()
            if not tool_name or not self.tools.has(tool_name):
                continue
            if self.approval_gate is not None:
                if not self.approval_gate.is_sensitive(tool_name):
                    continue
            elif tool_name not in {"exec", "spawn", "write_file", "edit_file"}:
                continue
            requested_tools.add(tool_name)
        return requested_tools

    @staticmethod
    def _build_one_shot_approval_reason(
        route_result: IntentRouteResult,
        approval_args: dict[str, Any],
    ) -> str:
        requested_tools = approval_args.get("approved_tools") or []
        tools_text = ", ".join(str(tool) for tool in requested_tools)
        return (
            f"One-shot explicit approval for intent '{route_result.intent_name or 'unknown'}' "
            f"to temporarily allow: {tools_text}"
        )

    @staticmethod
    def _contract_with_one_shot_approval(
        contract: IntentToolContract,
        approved_tools: set[str],
    ) -> IntentToolContract:
        allowed_tools = set(contract.allowed_tools) | set(approved_tools)
        denied_tools = {tool for tool in contract.denied_tools if tool not in approved_tools}
        preferred_tools = list(contract.preferred_tools)
        for tool in sorted(approved_tools):
            if tool not in preferred_tools:
                preferred_tools.append(tool)
        return IntentToolContract(
            intent_name=contract.intent_name,
            preferred_tools=preferred_tools,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            allow_constrained_replan=contract.allow_constrained_replan,
            allow_high_risk_escalation=contract.allow_high_risk_escalation,
            response_mode=contract.response_mode,
        )

    def _should_schedule_deferred_retry(
        self,
        msg: InboundMessage,
        route_result: IntentRouteResult,
    ) -> bool:
        if msg.channel == "cli":
            return False
        if route_result.route_status != "direct_failed":
            return False
        if route_result.intent_name not in {"weather", "exchange_rate", "fixed_site_fetch"}:
            return False
        diagnostic = str(route_result.diagnostic or "")
        retryable_prefixes = (
            "weather_sources_failed:",
            "exchange_sources_failed:",
            "fixed_site_failed:",
        )
        return diagnostic.startswith(retryable_prefixes)

    def _schedule_deferred_retry(
        self,
        msg: InboundMessage,
        route_result: IntentRouteResult,
        trace_id: str,
    ) -> None:
        task = asyncio.create_task(self._run_deferred_retry(msg, route_result, trace_id))
        self._deferred_retry_tasks.add(task)
        task.add_done_callback(self._deferred_retry_tasks.discard)

    async def _run_deferred_retry(
        self,
        msg: InboundMessage,
        route_result: IntentRouteResult,
        trace_id: str,
    ) -> None:
        try:
            if self._deferred_retry_delay_sec > 0:
                await asyncio.sleep(self._deferred_retry_delay_sec)

            session = self.sessions.get_or_create(msg.session_key)
            self._apply_channel_tool_policy(msg.channel)
            self._apply_channel_role_tool_policy(msg.metadata)
            self._apply_session_tool_policy(session.metadata)

            self.intent_router.set_control_context(session.metadata)
            try:
                retry_result = await self.intent_router.route(
                    msg.content,
                    tools=self.tools,
                    trace_id=trace_id,
                )
            finally:
                self.intent_router.set_control_context(None)
            if retry_result.route_status == "direct_success" and retry_result.content:
                followup_content = self._build_deferred_retry_followup(retry_result.content)
                await self._publish_followup_reply(
                    msg=msg,
                    session=session,
                    trace_id=trace_id,
                    content=followup_content,
                    metadata={
                        "deferred_retry": True,
                        "followup_kind": "success",
                        "intent_name": route_result.intent_name,
                    },
                )
                return
            if retry_result.route_status == "direct_failed" and retry_result.content:
                followup_content = self._build_deferred_retry_failure_followup(retry_result.content)
                await self._publish_followup_reply(
                    msg=msg,
                    session=session,
                    trace_id=trace_id,
                    content=followup_content,
                    metadata={
                        "deferred_retry": True,
                        "followup_kind": "failed",
                        "intent_name": route_result.intent_name,
                    },
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Deferred retry failed: {exc}")

    async def _publish_followup_reply(
        self,
        *,
        msg: InboundMessage,
        session: Any,
        trace_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add_message("assistant", content)
        self.sessions.save(session)
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata={
                    **TraceContext.child_metadata(trace_id),
                    **(metadata or {}),
                },
            )
        )

    @staticmethod
    def _build_deferred_retry_followup(content: str) -> str:
        return f"后台重试成功：{content}"

    @staticmethod
    def _build_deferred_retry_failure_followup(content: str) -> str:
        return f"后台重试后仍未成功：{content}"

    def _apply_session_tool_policy(self, metadata: dict[str, Any]) -> None:
        """Apply optional session-level allow/deny lists from session metadata."""
        allow = metadata.get("tools_allow")
        deny = metadata.get("tools_deny")

        if isinstance(allow, list) or isinstance(deny, list):
            allow_list = allow if isinstance(allow, list) else None
            deny_list = deny if isinstance(deny, list) else None
            self.tools.set_policy_scope("session", allow=allow_list, deny=deny_list)
        else:
            self.tools.clear_policy_scope("session")

    def _apply_channel_tool_policy(self, channel: str) -> None:
        """Apply optional channel-level allow/deny lists from config."""
        key = (channel or "").strip().lower()
        layer = self.tool_policy_config.channel_policies.get(key)
        if layer is None:
            self.tools.clear_policy_scope("channel")
            return
        self.tools.set_policy_scope("channel", allow=layer.allow, deny=layer.deny)

    def _apply_channel_role_tool_policy(self, metadata: dict[str, Any] | None) -> None:
        """
        Apply channel role policy: admin unrestricted, user restricted.
        Defaults to 'Deny-Closed' (blocking all tools) if role is missing or unknown.
        """
        role = str((metadata or {}).get("channel_role") or "").strip().lower()
        if role == "admin":
            self.tools.clear_policy_scope("channel_role")
            return

        if role == "user":
            allowed = set(self._CHANNEL_USER_ALLOW_TOOLS)
            deny = [name for name in self.tools.tool_names if name not in allowed]
            self.tools.set_policy_scope("channel_role", allow=None, deny=deny)
            return

        # Deny-Closed: block all tools if role is missing or invalid
        logger.warning(
            f"No valid role found in metadata (role='{role}'), enforcing Deny-Closed policy."
        )
        self.tools.set_policy_scope("channel_role", allow=None, deny=["*"])

    def _fail_closed_identity_reason(
        self, channel: str, metadata: dict[str, Any] | None
    ) -> str | None:
        """
        Enforce hard fail-closed identity checks before planning/execution.
        """
        channel_key = (channel or "").strip().lower()
        trusted_local_channels = {
            str(v).strip().lower()
            for v in (self.tool_policy_config.trusted_local_channels or [])
            if str(v).strip()
        }
        if is_trusted_local_surface(channel_key, trusted_local_channels):
            return None

        meta = metadata or {}
        channel_role = str(meta.get("channel_role") or "").strip().lower()
        if channel_role not in {"admin", "user", "guest"}:
            return "missing or invalid channel_role metadata"

        tenant_id = str(meta.get("tenant_id") or meta.get("tid") or "").strip()
        app_role = str(meta.get("role") or "").strip().lower()
        if not tenant_id or not app_role:
            return "missing tenant identity metadata (tenant_id/tid + role required)"
        sender_id = str(meta.get("sender_id") or "").strip()
        workspace_id = str(meta.get("workspace_id") or "").strip()
        agent_profile = str(meta.get("agent_profile") or "").strip().lower()
        if not sender_id:
            return "missing sender_id metadata"
        if not workspace_id:
            return "missing workspace_id metadata"
        if not agent_profile:
            return "missing agent_profile metadata"
        return None

    @staticmethod
    def _should_enforce_identity(metadata: dict[str, Any] | None) -> bool:
        meta = metadata or {}
        identity_keys = {"channel_role", "tenant_id", "tid", "role"}
        return bool(meta.get("identity_verified")) or any(k in meta for k in identity_keys)

    def _hydrate_identity_metadata(self, metadata: dict[str, Any]) -> None:
        channel_role = str(metadata.get("channel_role") or "").strip().lower()
        if channel_role in {"admin", "user", "guest"} and not str(metadata.get("role") or "").strip():
            metadata["role"] = channel_role
        if not str(metadata.get("tenant_id") or metadata.get("tid") or "").strip():
            metadata["tenant_id"] = "default"

    def _build_request_security_context(
        self,
        *,
        channel: str,
        sender_id: str,
        chat_id: str,
        metadata: dict[str, Any] | None,
        trace_id: str,
        session_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = dict(metadata or {})
        session_meta = session_metadata or {}
        tenant_id = str(
            meta.get("tenant_id") or meta.get("tid") or session_meta.get("tenant_id") or ""
        ).strip()
        role = str(meta.get("role") or session_meta.get("role") or "").strip().lower()
        trusted_local_channels = {
            str(v).strip().lower()
            for v in (self.tool_policy_config.trusted_local_channels or [])
            if str(v).strip()
        }
        return build_security_context(
            trace_id=str(trace_id or "").strip(),
            channel=str(channel or "").strip().lower(),
            sender_id=str(meta.get("sender_id") or sender_id or "").strip(),
            chat_id=str(chat_id or "").strip(),
            tenant_id=tenant_id or "default",
            workspace_id=str(
                meta.get("workspace_id") or normalize_workspace_id(self.workspace) or ""
            ).strip(),
            agent_profile=str(meta.get("agent_profile") or self.agent_id or "").strip().lower(),
            role=role,
            trust_level=(
                "trusted_local"
                if is_trusted_local_surface(channel, trusted_local_channels)
                else "remote_untrusted"
            ),
            origin_surface=str(meta.get("origin_surface") or channel or "").strip().lower(),
            channel_role=str(meta.get("channel_role") or "").strip().lower(),
            workspace_path=str(self.workspace),
            dev_profile=bool(self.dev_profile),
            trusted_local_only=bool(self.trusted_local_only),
            policy_snapshot=security_policy_snapshot(
                production_hardening=bool(self.tool_policy_config.production_hardening),
                legacy_compat=bool(self.tool_policy_config.legacy_compat),
                restrict_to_workspace=bool(self.restrict_to_workspace),
                profile_allowed_tools=list(self.profile_allowed_tools or []),
                profile_denied_tools=list(self.profile_denied_tools or []),
                extra={
                    "trusted_local_only": bool(self.trusted_local_only),
                    "dev_profile": bool(self.dev_profile),
                },
            ),
        )

    def _apply_intent_contract_policy(self, contract: IntentToolContract) -> None:
        self.tools.set_policy_scope(
            "intent_contract",
            allow=sorted(contract.allowed_tools),
            deny=sorted(contract.denied_tools),
        )

    @staticmethod
    def _build_intent_replan_instruction(route_result: IntentRouteResult) -> str:
        contract = route_result.contract
        if contract is None:
            return ""
        allowed = ", ".join(sorted(contract.allowed_tools))
        denied = ", ".join(sorted(contract.denied_tools))
        diagnostic = route_result.diagnostic or "direct route failed"
        return (
            f"The request matched intent '{contract.intent_name}'. "
            f"The deterministic route failed ({diagnostic}). "
            f"Retry using only these tools: {allowed}. "
            f"Do not use denied tools: {denied}. "
            "Answer normally once you have the result."
        )

    @staticmethod
    def _looks_like_permission_escalation_text(content: str | None) -> bool:
        text = str(content or "").lower()
        if not text:
            return False
        markers = (
            "approval",
            "permission restriction",
            "requires approval",
            "external commands",
            "unable to proceed without the necessary permissions",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _build_non_permission_route_failure(route_result: IntentRouteResult) -> str:
        if route_result.recovery_outcome is not None and route_result.recovery_outcome.content:
            return route_result.recovery_outcome.content
        if route_result.intent_name == "weather":
            return "暂时无法获取天气数据。这次失败不是权限或审批问题，而是安全路径下的数据请求未成功。请稍后重试。"
        return "当前安全路径执行未成功，这次失败不是权限或审批问题。请稍后重试。"

    async def _build_history_with_compression(
        self, session: "Session", trace_id: str
    ) -> list[dict[str, Any]]:
        """Compress long history with a rolling LLM summary when needed."""
        messages = session.messages
        summarized_upto = int(session.metadata.get("rolling_summary_upto", 0) or 0)
        plan = self.compressor.plan(messages, summarized_upto)
        should_compress_by_tokens = self._should_compress_by_token_budget(session, messages)
        if not should_compress_by_tokens and not plan.should_compress:
            return session.get_history()
        if (
            should_compress_by_tokens
            and not plan.should_compress
            and len(messages) > self.compressor.keep_recent
        ):
            # Token-based trigger asked for compression but turn-based window is quiet;
            # force a minimal safe prefix compression pass.
            cutoff = max(0, len(messages) - self.compressor.keep_recent)
            plan = self.compressor.plan(messages[:cutoff] + messages[cutoff:], summarized_upto)
            if not plan.should_compress:
                return session.get_history()

        prompt = self.compressor.build_prompt(
            plan.prefix_messages,
            str(session.metadata.get("rolling_summary", "")),
        )
        try:
            resp = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You summarize conversations for context compression.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=self.model,
                temperature=0.2,
                max_tokens=512,
            )
            summary = (resp.content or "").strip()
        except Exception as e:
            logger.warning(
                f"Context compression failed: {e} "
                + TraceContext.event_text(
                    "context.compress.error",
                    trace_id,
                    error_kind="runtime",
                    retryable=True,
                )
            )
            return session.get_history()

        if summary:
            session.metadata["rolling_summary"] = summary
            session.metadata["rolling_summary_upto"] = plan.summarized_upto
            session.metadata["compression_last_trigger_turn"] = len(messages)
            events = session.metadata.get("compression_events")
            if not isinstance(events, list):
                events = []
            events.append(
                {
                    "at_turn": len(messages),
                    "at_ms": int(datetime.now(UTC).timestamp() * 1000),
                    "reason": "token_budget" if should_compress_by_tokens else "turn_window",
                    "summarized_upto": plan.summarized_upto,
                }
            )
            session.metadata["compression_events"] = events[-50:]
            logger.info(
                "Context compression updated rolling summary "
                + TraceContext.event_text(
                    "context.compress.done",
                    trace_id,
                    summarized_upto=plan.summarized_upto,
                )
            )

        compressed_history: list[dict[str, Any]] = []
        rolling = str(session.metadata.get("rolling_summary", "")).strip()
        if rolling:
            compressed_history.append(self.compressor.build_summary_message(rolling))
        compressed_history.extend(
            {"role": m.get("role", "assistant"), "content": str(m.get("content", ""))}
            for m in plan.recent_messages
        )
        return compressed_history

    def _should_compress_by_token_budget(
        self, session: "Session", messages: list[dict[str, Any]]
    ) -> bool:
        if self.max_context_tokens <= 0:
            return False
        # Cooldown guard (hysteresis) to avoid oscillation.
        last_turn = int(session.metadata.get("compression_last_trigger_turn", -1) or -1)
        if last_turn >= 0 and (len(messages) - last_turn) < self.compression_cooldown_turns:
            return False

        est_tokens = self._estimate_context_tokens(messages)
        usage_ratio = float(est_tokens) / float(self.max_context_tokens)
        # Enter compression when above trigger ratio.
        if usage_ratio >= self.compression_trigger_ratio:
            return True
        # Stay quiet until usage goes below hysteresis floor.
        if usage_ratio <= self.compression_hysteresis_ratio:
            return False
        return False

    def _estimate_context_tokens(self, messages: list[dict[str, Any]]) -> int:
        count_fn = getattr(self.provider, "count_tokens", None)
        if callable(count_fn):
            try:
                counted = int(count_fn(messages=messages))
                if counted > 0:
                    return counted
            except Exception:
                pass
        return self.compressor.estimate_tokens_from_messages(messages)

    async def _extract_and_store_memory(
        self, user_text: str, assistant_text: str, trace_id: str
    ) -> None:
        """Extract durable memory with LLM and persist if needed."""
        if not self.memory_extractor.should_extract(user_text, assistant_text):
            return

        snapshot = self.context.memory.get_memory_context()
        prompt = self.memory_extractor.build_prompt(user_text, assistant_text, snapshot)
        try:
            resp = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "You extract durable memory from conversations."},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=self.model,
                temperature=0.1,
                max_tokens=220,
            )
        except Exception as e:
            logger.warning(
                f"Memory extraction failed: {e} "
                + TraceContext.event_text(
                    "memory.extract.error",
                    trace_id,
                    error_kind="runtime",
                    retryable=True,
                )
            )
            return

        extracted = self.memory_extractor.parse(resp.content)
        if not extracted.should_write:
            return

        try:
            if extracted.memory_type == "long_term":
                current = self.context.memory.read_long_term()
                if extracted.content in current:
                    return
                prefix = current.rstrip() + ("\n" if current.strip() else "")
                self.context.memory.write_long_term(prefix + f"- {extracted.content}\n")
            else:
                today = self.context.memory.read_today()
                if extracted.content in today:
                    return
                self.context.memory.append_today(f"- {extracted.content}")
        except Exception as e:
            logger.warning(
                f"Memory persistence failed: {e} "
                + TraceContext.event_text(
                    "memory.store.error",
                    trace_id,
                    error_kind="runtime",
                    retryable=False,
                )
            )
            return

        logger.info(
            "Memory extracted and stored "
            + TraceContext.event_text(
                "memory.extract.done",
                trace_id,
                memory_type=extracted.memory_type,
            )
        )

    async def _run_plan_phase(
        self,
        messages: list[dict[str, Any]],
        user_goal: str,
        trace_id: str,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a lightweight planning pass before tool execution."""
        if not self.execution.should_plan():
            return messages

        active_model = model or self._resolve_run_model(messages)
        prompt = self.execution.build_plan_prompt(user_goal)
        plan_input = messages + [{"role": "user", "content": prompt}]
        try:
            plan, _used_model, _fallback_state = await self._chat_with_optional_fallback(
                messages=plan_input,
                tools=None,
                model=active_model,
                allow_fallback=bool(self.fallback_model and active_model != self.fallback_model),
                trace_id=trace_id,
            )
        except Exception as e:
            logger.warning(
                f"Planning phase failed: {e} "
                + TraceContext.event_text(
                    "agent.plan.error",
                    trace_id,
                    error_kind="runtime",
                    retryable=False,
                )
            )
            return messages

        if plan.content:
            logger.info(
                "Planning phase generated plan "
                + TraceContext.event_text("agent.plan.done", trace_id)
            )
            return self.context.add_assistant_message(messages, f"[Plan]\n{plan.content}")
        return messages

    async def _chat_with_optional_fallback(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        allow_fallback: bool,
        trace_id: str,
    ) -> tuple[LLMResponse, str, str]:
        response = await self.provider.chat(
            messages=messages,
            tools=tools,
            model=model,
        )
        if (
            response.finish_reason != "error"
            or not allow_fallback
            or not self.fallback_model
            or self.fallback_model == model
        ):
            return response, model, "primary"

        logger.warning(
            "Primary model returned error; retrying once with fallback model "
            + TraceContext.event_text(
                "agent.model.fallback",
                trace_id,
                error_kind="runtime",
                retryable=True,
                from_model=model,
                to_model=self.fallback_model,
            )
        )
        fallback_response = await self.provider.chat(
            messages=messages,
            tools=tools,
            model=self.fallback_model,
        )
        if fallback_response.finish_reason == "error":
            return fallback_response, self.fallback_model, f"fallback_error:{model}"
        return fallback_response, self.fallback_model, f"fallback_model:{model}"

    async def _run_execute_reflect_loop(
        self,
        messages: list[dict[str, Any]],
        trace_id: str,
        session: "Session | None" = None,
        model: str | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        usage_collector: dict[str, int] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        approved_one_shot_tools: set[str] | None = None,
        active_intent_contract: IntentToolContract | None = None,
        allow_model_fallback: bool = False,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Run execute loop with bounded reflection retries and mid-turn hot-swapping."""
        iteration = 0
        reflections_used = 0
        final_content: str | None = None
        pending_param_failures: dict[str, tuple[dict[str, Any], str]] = {}
        active_model = model or self._resolve_run_model(messages)
        trace_summary: dict[str, Any] = {
            "iterations": 0,
            "tool_batches": 0,
            "tool_calls": 0,
            "tool_successes": 0,
            "tool_failures": 0,
            "reflections_used": 0,
            "reflection_triggered": False,
            "approval_waited": False,
            "reload_triggered": False,
            "fallback_used": False,
            "fallback_reason": "",
            "models_used": [],
            "executed_tools": [],
            "last_error_kind": "",
            "last_error_code": "",
        }
        self._last_execution_trace_summary = None

        while iteration < self.max_iterations:
            iteration += 1
            trace_summary["iterations"] = iteration
            try:
                response, used_model, fallback_state = await self._chat_with_optional_fallback(
                    messages=messages,
                    tools=tool_definitions
                    if tool_definitions is not None
                    else (
                        self.tools.get_visible_definitions()
                        if hasattr(self.tools, "get_visible_definitions")
                        else self.tools.get_definitions()
                    ),
                    model=active_model,
                    allow_fallback=allow_model_fallback,
                    trace_id=trace_id,
                )
                active_model = used_model
                self._last_run_model_used = active_model
                self._last_run_model_reason = fallback_state
                models_used = trace_summary.setdefault("models_used", [])
                if active_model and active_model not in models_used:
                    models_used.append(active_model)
                if str(fallback_state or "").startswith("fallback_"):
                    trace_summary["fallback_used"] = True
                    trace_summary["fallback_reason"] = str(fallback_state)
                self._accumulate_usage(usage_collector, response.usage)

                if not response.has_tool_calls:
                    final_content = response.content
                    break

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                tool_results: list[ToolResult] = []
                reload_triggered = False
                approval_blocked_cli = False
                trace_summary["tool_batches"] = int(trace_summary.get("tool_batches", 0)) + 1
                for tool_call in response.tool_calls:
                    approved = None
                    trace_summary["tool_calls"] = int(trace_summary.get("tool_calls", 0)) + 1
                    raw_args = dict(tool_call.arguments)
                    call_args = self._maybe_rewrite_tool_args(
                        tool_call.name, dict(raw_args), messages
                    )
                    executed_tools = trace_summary.setdefault("executed_tools", [])
                    if len(executed_tools) < 8:
                        executed_tools.append(str(tool_call.name or ""))
                    contract_violation = self._evaluate_intent_contract_tool_use(
                        active_intent_contract,
                        tool_call.name,
                        approved_one_shot_tools or set(),
                    )
                    if contract_violation is not None:
                        result = ToolResult.failure(
                            ToolErrorKind.PERMISSION,
                            contract_violation,
                            code="intent_contract_denied",
                        )
                        trace_summary["tool_failures"] = int(trace_summary.get("tool_failures", 0)) + 1
                        trace_summary["last_error_kind"] = ToolErrorKind.PERMISSION.value
                        trace_summary["last_error_code"] = "intent_contract_denied"
                        tool_results.append(result)
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                        continue
                    one_shot_approved = tool_call.name in (approved_one_shot_tools or set())
                    if (
                        self.approval_gate
                        and self.approval_gate.is_sensitive(tool_call.name)
                        and not one_shot_approved
                    ):
                        session_id = session.key if session is not None else f"{channel}:{chat_id}"
                        approved = self.approval_gate.consume_approved(
                            session_id, tool_call.name, call_args
                        )
                        if approved and self._audit_worker:
                            await self._audit_worker.audit_turn(
                                trace_id,
                                {
                                    "event_type": "tool.approval_consumed",
                                    "tool": tool_call.name,
                                    "identity": self.tools._audit_identity_summary(),
                                    "policy_snapshot": dict(
                                        self.tools._request_context.get("policy_snapshot") or {}
                                    ),
                                    "approval": {
                                        "approval_id": approved.approval_id,
                                        "status": approved.status.value,
                                    },
                                },
                            )
                        if not approved:
                            request_security_context = dict(self.tools._request_context)
                            approval = await self.approval_gate.request_approval(
                                session_id=session_id,
                                tool_name=tool_call.name,
                                tool_args=call_args,
                                reason="Sensitive operation",
                                bus=self.bus,
                                channel=channel,
                                chat_id=chat_id,
                                trace_id=trace_id,
                                capability=tool_call.name,
                                resource_scope={
                                    "tool_name": tool_call.name,
                                    "tool_args": call_args,
                                },
                                security_context=request_security_context,
                                policy_snapshot_hash=str(
                                    request_security_context.get("policy_snapshot_hash")
                                    or ((request_security_context.get("policy_snapshot") or {}).get("policy_snapshot_hash"))
                                    or ""
                                ),
                                sidecar_target=str(
                                    getattr(getattr(self.tools.get(tool_call.name), "sidecar_url", None), "strip", lambda: "")()
                                    if self.tools.has(tool_call.name)
                                    else ""
                                ),
                                decision_basis="Sensitive operation",
                            )
                            if self._audit_worker:
                                await self._audit_worker.audit_turn(
                                    trace_id,
                                    {
                                        "event_type": "tool.approval_requested",
                                        "tool": tool_call.name,
                                        "identity": self.tools._audit_identity_summary(),
                                        "policy_snapshot": dict(
                                            self.tools._request_context.get("policy_snapshot") or {}
                                        ),
                                        "approval": {
                                            "approval_id": approval.approval_id,
                                            "status": approval.status.value,
                                        },
                                    },
                                )
                            approval_msg = approval.format_request_message()
                            result = ToolResult.failure(
                                ToolErrorKind.PERMISSION,
                                approval_msg,
                                code="approval_required",
                            )
                            trace_summary["tool_failures"] = int(trace_summary.get("tool_failures", 0)) + 1
                            trace_summary["approval_waited"] = True
                            trace_summary["last_error_kind"] = ToolErrorKind.PERMISSION.value
                            trace_summary["last_error_code"] = "approval_required"
                            # In direct CLI mode there is no outbound dispatcher consuming bus messages.
                            # Surface approval instructions immediately to the user.
                            if channel == "cli":
                                final_content = approval_msg
                                approval_blocked_cli = True
                            tool_results.append(result)
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_call.name, result
                            )
                            break
                    try:
                        result = await self.tools.execute(
                            tool_call.name, call_args, trace_id=trace_id
                        )
                        if approved and self.approval_gate is not None:
                            self.approval_gate.record_execution_finished(
                                approved,
                                ok=result.ok,
                                error_code=str(result.error.code if result.error else ""),
                            )
                    except AgentMidTurnReloadException as e:
                        logger.info(f"Mid-turn reload triggered: {e.message}")
                        if session is not None and "skill_pins" not in session.metadata:
                            session.metadata["skill_pins"] = {}
                        if session is not None:
                            session.metadata["skill_pins"].update(e.pins or {})
                        result = ToolResult.success(
                            f"SUCCESS: {e.message}. System context reloaded."
                        )
                        reload_triggered = True
                        trace_summary["reload_triggered"] = True
                    # Continue to append result message before breaking to preserve traceability.

                    # Record learning and update messages
                    if not result.ok and result.error and result.error.kind.value == "parameter":
                        pending_param_failures[tool_call.name] = (
                            dict(raw_args),
                            result.error.message,
                        )
                    elif result.ok and tool_call.name in pending_param_failures:
                        failed_args, error_message = pending_param_failures.pop(tool_call.name)
                        self._record_tool_learning(
                            tool_call.name, failed_args, dict(call_args), error_message, trace_id
                        )

                    if result.ok:
                        trace_summary["tool_successes"] = (
                            int(trace_summary.get("tool_successes", 0)) + 1
                        )
                    else:
                        trace_summary["tool_failures"] = (
                            int(trace_summary.get("tool_failures", 0)) + 1
                        )
                        if result.error is not None:
                            trace_summary["last_error_kind"] = str(result.error.kind.value)
                            trace_summary["last_error_code"] = str(result.error.code or "")
                    tool_results.append(result)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                    if (
                        result.error and result.error.code == "approval_required"
                    ) or reload_triggered:
                        break
            except Exception as e:
                logger.error(f"Error in execution loop: {e}")
                break

            if approval_blocked_cli:
                break

            if reload_triggered:
                continue

            hints = self.execution.collect_error_hints(tool_results)
            if hints and self.execution.can_reflect(reflections_used):
                reflections_used += 1
                trace_summary["reflections_used"] = reflections_used
                trace_summary["reflection_triggered"] = True
                reflection_prompt = self.execution.build_reflection_prompt(hints)
                messages.append({"role": "user", "content": reflection_prompt})
                continue
            trace_summary["reflections_used"] = reflections_used
        self._last_execution_trace_summary = dict(trace_summary)
        return final_content, messages

    @staticmethod
    def _evaluate_intent_contract_tool_use(
        contract: IntentToolContract | None,
        tool_name: str,
        approved_one_shot_tools: set[str],
    ) -> str | None:
        if contract is None:
            return None
        tool = str(tool_name or "").strip().lower()
        if not tool:
            return None
        if tool in contract.denied_tools and tool not in approved_one_shot_tools:
            return (
                f"Tool '{tool}' denied by intent contract for '{contract.intent_name}'. "
                "This request cannot escalate through approval for that tool."
            )
        if tool in approved_one_shot_tools:
            return None
        if tool in contract.allowed_tools:
            return None
        if contract.allow_high_risk_escalation:
            return (
                f"Tool '{tool}' is outside the approved one-shot scope for intent '{contract.intent_name}'. "
                "Only explicitly approved tools may be used."
            )
        return (
            f"Tool '{tool}' is outside the allowed tool contract for intent '{contract.intent_name}'. "
            "This request cannot escalate through approval."
        )

    def _maybe_rewrite_tool_args(
        self,
        tool_name: str,
        call_args: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.auto_parameter_rewrite:
            return call_args
        user_query = self._latest_user_text(messages)
        suggested = self.context.memory.suggest_tool_arg_rewrite(
            tool_name=tool_name,
            args=call_args,
            query=user_query,
        )
        return suggested or call_args

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = str(block.get("text") or "").strip()
                        if t:
                            texts.append(t)
                if texts:
                    return "\n".join(texts)
        return ""

    def _record_tool_learning(
        self,
        tool_name: str,
        failed_args: dict[str, Any],
        corrected_args: dict[str, Any],
        error_message: str,
        trace_id: str,
    ) -> None:
        """Persist successful parameter-correction pattern for future prompt guidance."""
        try:
            memory_dir = self.workspace / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            path = memory_dir / "TOOLS_LEARNING.md"
            if not path.exists():
                path.write_text(
                    "# Tool Learning\n\n"
                    "Successful parameter correction patterns observed during reflection.\n\n",
                    encoding="utf-8",
                )
            sig = self._tool_learning_signature(tool_name, failed_args, corrected_args)
            existing_tail = path.read_text(encoding="utf-8")[-20000:]
            if f"sig={sig}" in existing_tail:
                return
            line = (
                f"- {datetime.now(UTC).isoformat().replace('+00:00', 'Z')} "
                f"tool={tool_name} "
                f"sig={sig} "
                f"error={json.dumps(error_message, ensure_ascii=False)} "
                f"from={json.dumps(failed_args, ensure_ascii=False)} "
                f"to={json.dumps(corrected_args, ensure_ascii=False)} "
                f"trace_id={trace_id}\n"
            )
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.warning(
                f"Tool learning write failed: {e} "
                + TraceContext.event_text(
                    "tool.learning.error",
                    trace_id,
                    tool=tool_name,
                    error_kind="runtime",
                    retryable=False,
                )
            )

    @staticmethod
    def _tool_learning_signature(
        tool_name: str, failed_args: dict[str, Any], corrected_args: dict[str, Any]
    ) -> str:
        payload = json.dumps(
            {"tool": tool_name, "from": failed_args, "to": corrected_args},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def _resolve_run_model(
        self,
        messages: list[dict[str, Any]],
        preferred_model: str | None = None,
        *,
        intent_name: str | None = None,
        think_enabled: bool = False,
        allow_dynamic_override: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Use vision model when current message payload includes image blocks."""
        if self.vision_model and self._contains_image_payload(messages):
            return self.vision_model
        if not allow_dynamic_override:
            return str(preferred_model or self.model)
        routing_hint = self._resolve_model_routing_hint(metadata)
        task_type = routing_hint.get("task_type") or ""
        if task_type:
            override = str(self.task_type_model_overrides.get(task_type) or "").strip()
            if override:
                return override
        normalized_intent = str(intent_name or "").strip().lower()
        if normalized_intent:
            override = str(self.intent_model_overrides.get(normalized_intent) or "").strip()
            if override:
                return override
        if routing_hint.get("stability_required") and self.stability_model:
            return self.stability_model
        if routing_hint.get("cost_sensitive") and self.cost_model:
            return self.cost_model
        if think_enabled and self.thinking_model:
            return self.thinking_model
        return str(preferred_model or self.model)

    def _resolve_run_model_reason(
        self,
        messages: list[dict[str, Any]],
        *,
        intent_name: str | None = None,
        think_enabled: bool = False,
        allow_dynamic_override: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if self.vision_model and self._contains_image_payload(messages):
            return "vision_model"
        if not allow_dynamic_override:
            return "session_override_model"
        routing_hint = self._resolve_model_routing_hint(metadata)
        task_type = routing_hint.get("task_type") or ""
        if task_type and str(self.task_type_model_overrides.get(task_type) or "").strip():
            return f"task_type_override:{task_type}"
        normalized_intent = str(intent_name or "").strip().lower()
        if normalized_intent and str(self.intent_model_overrides.get(normalized_intent) or "").strip():
            return f"intent_override:{normalized_intent}"
        if routing_hint.get("stability_required") and self.stability_model:
            return "stability_model"
        if routing_hint.get("cost_sensitive") and self.cost_model:
            return "cost_model"
        if think_enabled and self.thinking_model:
            return "thinking_model"
        return "default_model"

    @staticmethod
    def _resolve_model_routing_hint(metadata: dict[str, Any] | None) -> dict[str, Any]:
        meta = metadata if isinstance(metadata, dict) else {}
        task_type = str(meta.get("task_type") or "").strip().lower()
        profile_hint = str(meta.get("model_profile") or meta.get("routing_profile") or "").strip().lower()
        cost_tier = str(meta.get("cost_tier") or "").strip().lower()
        stability_tier = str(meta.get("stability_tier") or "").strip().lower()
        cost_sensitive = bool(meta.get("cost_sensitive")) or profile_hint in {
            "cheap",
            "cost",
            "low_cost",
        } or cost_tier in {"cheap", "low", "budget"}
        stability_required = bool(meta.get("stability_required")) or profile_hint in {
            "stable",
            "stability",
            "reliable",
        } or stability_tier in {"stable", "high", "reliable"}
        return {
            "task_type": task_type,
            "cost_sensitive": cost_sensitive,
            "stability_required": stability_required,
        }

    @staticmethod
    def _accumulate_usage(target: dict[str, int] | None, usage: dict[str, Any] | None) -> None:
        if target is None or not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, int):
                target[key] = int(target.get(key, 0)) + value

    @staticmethod
    def _contains_image_payload(messages: list[dict[str, Any]]) -> bool:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image_url":
                    return True
                if block.get("type") == "text":
                    text = str(block.get("text") or "").lower()
                    if "attached media references" in text and "/image/" in text:
                        if (
                            "media://" in text
                            or "feishu://" in text
                            or "whatsapp://" in text
                            or "telegram://" in text
                            or "discord://" in text
                        ):
                            return True
        return False

    _CHANNEL_USER_ALLOW_TOOLS = ["read_file", "web_search"]
    _HIGH_RISK_SKILL_SCOPES = {"exec", "message", "cron", "sessions"}
    _GATE3_DEFAULT_CONTRACT = IntentToolContract(
        intent_name="default_contract",
        preferred_tools=["read_file", "list_dir", "web_search", "web_fetch", "message"],
        allowed_tools={"read_file", "list_dir", "web_search", "web_fetch", "message"},
        denied_tools={
            "exec",
            "spawn",
            "write_file",
            "edit_file",
            "cron",
            "sessions_spawn",
            "sessions_kill",
            "sessions_write",
            "sessions_signal",
            "sessions_resize",
        },
        intent_mode="skill_first",
        allow_constrained_replan=True,
        allow_high_risk_escalation=False,
        response_mode="llm_assisted",
    )
