"""CLI commands for zen_claw."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from zen_claw import __logo__, __version__
from zen_claw.utils.netguard import is_public_ip, resolve_safe_ip

console = Console()


def _gateway_health_payload() -> dict[str, str]:
    return {"status": "ok", "service": "zen-claw", "surface": "gateway"}


def _require_gateway_api_key(headers: Any, send_json: Any) -> bool:
    from zen_claw.dashboard.server import verify_api_key

    api_key = str(headers.get("X-API-Key", ""))
    if api_key and verify_api_key(api_key):
        return True
    send_json(
        401,
        {
            "error": "invalid_api_key",
            "detail": "Provide a valid API key via the X-API-Key header.",
        },
    )
    return False


def _gateway_agents_payload() -> dict[str, Any]:
    from zen_claw.agent.pool import list_agent_profiles
    from zen_claw.config.loader import get_data_dir, load_config
    from zen_claw.dashboard.server import _collect_pending_apply_state

    cfg = load_config()
    data_dir = get_data_dir()
    rows = [
        {
            "agent_id": row.agent_id,
            "display_name": row.display_name,
            "description": row.description,
            "model": row.model,
            "vision_model": row.vision_model,
            "thinking_model": row.thinking_model,
            "fallback_model": row.fallback_model,
            "cost_model": row.cost_model,
            "stability_model": row.stability_model,
            "planning_enabled": bool(row.enable_planning),
            "workspace": str(row.workspace),
            "registered": bool(row.is_registered),
            "intent_model_overrides": dict(row.intent_model_overrides or {}),
            "task_type_model_overrides": dict(row.task_type_model_overrides or {}),
            "allowed_models": list(row.allowed_models or []),
            "routing_keywords": list(
                getattr(cfg.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
            ),
            "routing_keywords_count": len(
                getattr(cfg.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
            ),
            "skill_names": list(row.skill_names or []),
            "skill_count": len(row.skill_names or []),
            "allowed_tools_count": len(row.allowed_tools or []),
            "denied_tools_count": len(row.denied_tools or []),
        }
        for row in list_agent_profiles(cfg)
    ]
    pending_state = _collect_pending_apply_state(
        data_dir=data_dir,
        actions_path=data_dir / "dashboard" / "agent_actions.log.jsonl",
        apply_path=data_dir / "dashboard" / "agent_apply.log.jsonl",
        target_field="agent_id",
    )
    recent_actions = list(pending_state["recent_actions"])
    pending = list(pending_state["pending"])
    return {
        "agents": rows,
        "total": len(rows),
        "recent_actions": recent_actions[:5],
        "actions_total": len(recent_actions),
        "pending_reload": len(pending) > 0,
        "pending_reload_count": len(pending),
        "last_apply": pending_state["last_apply"],
    }


def _start_gateway_health_server(
    *,
    port: int,
    host: str = "127.0.0.1",
    webhook_trigger_channel: Any | None = None,
) -> ThreadingHTTPServer:
    from zen_claw.dashboard.server import _invoke_agent_text, _read_agent_profile_detail
    from zen_claw.dashboard.webhooks import _append_workflow_webhook_event

    class _GatewayHealthHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/v1/health":
                self._send_json(200, _gateway_health_payload())
                return
            if path == "/api/v1/agents":
                if not _require_gateway_api_key(self.headers, self._send_json):
                    return
                self._send_json(200, _gateway_agents_payload())
                return
            agents_prefix = "/api/v1/agents/"
            if path.startswith(agents_prefix):
                if not _require_gateway_api_key(self.headers, self._send_json):
                    return
                agent_id = path[len(agents_prefix) :].strip("/")
                if not agent_id or "/" in agent_id:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                try:
                    detail = json.loads(_read_agent_profile_detail(agent_id=agent_id).body.decode("utf-8"))
                except Exception:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                self._send_json(200, detail)
                return
            if path == "/healthz":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path != "/api/v1/agent/invoke":
                webhook_prefix = "/webhook/trigger/"
                if path.startswith(webhook_prefix):
                    agent_id = path[len(webhook_prefix) :].strip("/")
                    if not agent_id:
                        self.send_response(404)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b"not found")
                        return
                    if webhook_trigger_channel is None:
                        self._send_json(
                            503,
                            {"success": False, "reason": "webhook_trigger_channel_not_enabled"},
                        )
                        return
                    self._handle_webhook_trigger(
                        agent_id=agent_id,
                        parsed=parsed,
                        webhook_trigger_channel=webhook_trigger_channel,
                        append_audit=_append_workflow_webhook_event,
                    )
                    return
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found")
                return

            if not _require_gateway_api_key(self.headers, self._send_json):
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                content_length = 0
            raw_body = self.rfile.read(content_length)
            try:
                payload = json.loads(raw_body.decode("utf-8") or "{}")
            except Exception:
                self._send_json(400, {"error": "invalid_json"})
                return

            message = str(payload.get("message", ""))
            if not message.strip():
                self._send_json(400, {"error": "message_required"})
                return

            session_id = str(payload.get("session_id") or uuid.uuid4())
            text = asyncio.run(_invoke_agent_text(message, session_id))
            self._send_json(200, {"response": text, "session_id": session_id})

        def _handle_webhook_trigger(
            self,
            *,
            agent_id: str,
            parsed: Any,
            webhook_trigger_channel: Any,
            append_audit: Any,
        ) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                content_length = 0
            body_bytes = self.rfile.read(content_length)
            headers = {str(k).lower(): str(v) for k, v in self.headers.items()}
            client_ip = str(self.client_address[0] if self.client_address else "")
            ok, reason = webhook_trigger_channel.validate_request(
                body=body_bytes,
                headers=headers,
                client_ip=client_ip,
            )
            if not ok:
                self._send_json(403, {"success": False, "reason": reason})
                return

            payload: Any = {}
            if body_bytes:
                ctype = str(self.headers.get("Content-Type", "")).lower()
                try:
                    if "application/json" in ctype:
                        payload = json.loads(body_bytes.decode("utf-8"))
                    elif (
                        "application/x-www-form-urlencoded" in ctype
                        or "multipart/form-data" in ctype
                    ):
                        payload = {k: v for k, v in parse_qsl(body_bytes.decode("utf-8"))}
                    else:
                        text = body_bytes.decode("utf-8", errors="ignore").strip()
                        if text:
                            payload = {"content": text}
                except Exception:
                    payload = {"content": body_bytes.decode("utf-8", errors="ignore")}
            query_map = {str(k): str(v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
            accepted = asyncio.run(
                webhook_trigger_channel.ingest_trigger(
                    agent_id=agent_id,
                    payload=payload,
                    headers=headers,
                    query=query_map,
                    client_ip=client_ip,
                    metadata={"source": "gateway.webhooks.trigger"},
                )
            )
            append_audit(agent_id, accepted, client_ip)
            self._send_json(
                202,
                {
                    "success": True,
                    "agent_id": agent_id,
                    "trace_id": accepted.get("trace_id", ""),
                    "workflow_source": accepted.get("workflow_source", ""),
                    "workflow_run_id": accepted.get("workflow_run_id", ""),
                    "workflow_step": accepted.get("workflow_step", ""),
                },
            )

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), _GatewayHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _display_logo() -> str:
    """Return a console-safe logo for the active terminal encoding."""
    encoding = getattr(console.file, "encoding", None) or os.device_encoding(1) or "utf-8"
    try:
        __logo__.encode(encoding)
    except Exception:
        return "[zen-claw]"
    return __logo__


app = typer.Typer(
    name="zen-claw",
    help=f"{_display_logo()} zen-claw - Personal AI Assistant",
    no_args_is_help=True,
)


async def _execute_knowledge_cron_job(job: Any, *, data_dir: Path) -> str:
    from zen_claw.agent.tools.knowledge import KnowledgeAddTool
    from zen_claw.config.loader import load_config
    from zen_claw.knowledge.pipeline import RAGPipeline

    dashboard_dir = Path(data_dir) / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    log_path = dashboard_dir / "knowledge_cron.log.jsonl"
    source = str(getattr(job.payload, "knowledge_source", "") or "").strip()
    notebook = str(getattr(job.payload, "knowledge_notebook", "") or "").strip() or "default"
    cfg = load_config()
    max_documents = int(
        getattr(job.payload, "knowledge_retention_max_documents", None)
        or cfg.knowledge.retention_max_documents
        or 0
    )
    max_age_days = int(
        getattr(job.payload, "knowledge_retention_max_age_days", None)
        or cfg.knowledge.retention_max_age_days
        or 0
    )

    if not source and max_documents <= 0 and max_age_days <= 0:
        raise ValueError("knowledge_source or retention policy is required")

    payload: dict[str, Any] = {}
    if source:
        tool = KnowledgeAddTool(data_dir=data_dir)
        result = await tool.execute(source=source, notebook_id=notebook)
        if not result.ok:
            message = result.error.message if result.error else "knowledge cron ingest failed"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at_ms": int(time.time() * 1000),
                            "job_id": str(getattr(job, "id", "") or ""),
                            "job_name": str(getattr(job, "name", "") or ""),
                            "knowledge_source": source,
                            "knowledge_notebook": notebook,
                            "retention_max_documents": max_documents,
                            "retention_max_age_days": max_age_days,
                            "status": "error",
                            "error": message,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            raise RuntimeError(message)
        payload = json.loads(result.content)

    retention_payload: dict[str, Any] = {}
    if max_documents > 0 or max_age_days > 0:
        retention_payload = RAGPipeline(data_dir).run_retention(
            notebook_id=notebook,
            max_documents=max_documents,
            max_age_days=max_age_days,
        )
        if payload:
            payload["retention"] = retention_payload
        else:
            payload = retention_payload

    if not payload:
        payload = {"notebook": notebook, "documents": 0, "chunks_added": 0}

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at_ms": int(time.time() * 1000),
                    "job_id": str(getattr(job, "id", "") or ""),
                    "job_name": str(getattr(job, "name", "") or ""),
                    "knowledge_source": source,
                    "knowledge_notebook": notebook,
                    "retention_max_documents": max_documents,
                    "retention_max_age_days": max_age_days,
                    "status": "ok",
                    "documents": int(payload.get("documents", 0) or 0),
                    "chunks_added": int(payload.get("chunks_added", 0) or 0),
                    "retention_documents_removed": int(
                        retention_payload.get("documents_removed", 0) or 0
                    ),
                    "retention_chunks_deleted": int(
                        retention_payload.get("chunks_deleted", 0) or 0
                    ),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return json.dumps(payload, ensure_ascii=False)


async def _execute_crawler_cron_job(job: Any, *, data_dir: Path) -> str:
    from zen_claw.skills.crawler.extractor import CrawlerExtractor, CrawlerSource

    dashboard_dir = Path(data_dir) / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    log_path = dashboard_dir / "crawler_cron.log.jsonl"
    source_url = str(getattr(job.payload, "crawler_source_url", "") or "").strip()
    if not source_url:
        raise ValueError("crawler_source_url is required")
    metadata_raw = str(getattr(job.payload, "crawler_metadata_json", "") or "").strip()
    metadata = json.loads(metadata_raw) if metadata_raw else {}
    if metadata and not isinstance(metadata, dict):
        raise ValueError("crawler_metadata_json must be a JSON object")
    source = CrawlerSource(
        name=str(getattr(job.payload, "crawler_source_name", "") or "").strip() or str(getattr(job, "name", "") or "").strip() or "crawler",
        url=source_url,
        notebook_id=str(getattr(job.payload, "crawler_notebook", "") or "").strip() or "default",
        selector=str(getattr(job.payload, "crawler_selector", "") or "").strip(),
        use_browser=bool(getattr(job.payload, "crawler_use_browser", False)),
        max_chars=max(100, int(getattr(job.payload, "crawler_max_chars", 0) or 20_000)),
        metadata=metadata,
    )
    tenant_id = str(getattr(job.payload, "crawler_tenant_id", "") or "").strip() or "default"
    store_backend = str(getattr(job.payload, "crawler_store_backend", "") or "").strip()
    extractor = CrawlerExtractor(data_dir=data_dir, tenant_id=tenant_id, store_kind=store_backend)
    try:
        payload = await extractor.crawl_to_rag(source)
    except Exception as exc:
        _append_jsonl(
            log_path,
            {
                "at_ms": int(time.time() * 1000),
                "job_id": str(getattr(job, "id", "") or ""),
                "job_name": str(getattr(job, "name", "") or ""),
                "source_name": source.name,
                "source_url": source.url,
                "notebook": source.notebook_id,
                "tenant_id": tenant_id,
                "store_backend": store_backend,
                "use_browser": bool(source.use_browser),
                "status": "error",
                "error": str(exc),
            },
        )
        raise
    _append_jsonl(
        log_path,
        {
            "at_ms": int(time.time() * 1000),
            "job_id": str(getattr(job, "id", "") or ""),
            "job_name": str(getattr(job, "name", "") or ""),
            "source_name": source.name,
            "source_url": source.url,
            "notebook": payload.get("notebook", source.notebook_id),
            "tenant_id": payload.get("tenant_id", tenant_id),
            "store_backend": payload.get("store_backend", store_backend),
            "use_browser": bool(source.use_browser),
            "chunks_added": int(payload.get("chunks_added", 0) or 0),
            "documents": int(payload.get("documents", 0) or 0),
            "status": "ok",
        },
    )
    return json.dumps(payload, ensure_ascii=False)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _resolve_routed_agent(
    router,
    *,
    channel: str,
    chat_id: str,
    user_id: str,
    content: str,
    explicit_agent_id: str = "",
):
    """Resolve target agent through the orchestration router."""
    return router.resolve(
        channel=channel,
        chat_id=chat_id,
        user_id=user_id,
        content=content,
        explicit_agent_id=explicit_agent_id,
    )


async def _process_inbound_with_agent_router(
    msg,
    *,
    router,
    default_agent,
    agent_pool,
    bus,
    channel_manager=None,
) -> None:
    """Dispatch one inbound message to the routed agent loop and publish its reply."""
    from zen_claw.observability.trace import TraceContext

    explicit_agent_id = str((msg.metadata or {}).get("agent_id") or "").strip()
    decision = _resolve_routed_agent(
        router,
        channel=msg.channel,
        chat_id=msg.chat_id,
        user_id=msg.sender_id,
        content=msg.content,
        explicit_agent_id=explicit_agent_id,
    )
    target_loop = (
        default_agent
        if decision.agent_id == "default"
        else await agent_pool.get_or_create(decision.agent_id)
    )

    if not isinstance(msg.metadata, dict):
        msg.metadata = {}
    msg.metadata["routed_agent_id"] = decision.agent_id
    msg.metadata["routed_agent_reason"] = decision.reason
    if decision.matched_keyword:
        msg.metadata["routed_agent_keyword"] = decision.matched_keyword

    if (
        channel_manager is not None
        and msg.channel not in {"cli", "system"}
        and str(msg.sender_id or "").strip()
        and decision.agent_id != "default"
        and decision.reason in {"keyword", "channel_profile"}
    ):
        channel_manager.bind_agent(
            channel=msg.channel,
            chat_id=msg.chat_id,
            user_id=msg.sender_id,
            agent_id=decision.agent_id,
            reason=f"auto_route:{decision.reason}",
        )

    try:
        response = await target_loop._process_message(msg)
        if response:
            await bus.publish_outbound(response)
    except Exception as exc:
        trace_id = msg.trace_id or TraceContext.new_trace_id()
        logger.error(
            f"Error processing routed message: {exc} "
            + TraceContext.event_text(
                "agent.route.process.error",
                trace_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                agent_id=decision.agent_id,
                error_kind="runtime",
                retryable=False,
            )
        )
        from zen_claw.bus.events import OutboundMessage

        await bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Sorry, I encountered an error: {str(exc)}",
                metadata=TraceContext.child_metadata(trace_id),
            )
        )


def _cron_job_has_knowledge_ingest(job: Any) -> bool:
    return bool(
        str(getattr(job.payload, "knowledge_source", "") or "").strip()
        or int(getattr(job.payload, "knowledge_retention_max_documents", 0) or 0) > 0
        or int(getattr(job.payload, "knowledge_retention_max_age_days", 0) or 0) > 0
    )


def version_callback(value: bool):
    if value:
        Console().print(f"{_display_logo()} zen-claw v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """zen-claw - Personal AI Assistant."""
    global console
    console = Console()


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize zen-claw configuration and workspace."""
    from zen_claw.config.loader import get_config_path, save_config
    from zen_claw.config.schema import Config
    from zen_claw.utils.helpers import get_workspace_path

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Create default bootstrap files
    _create_workspace_templates(workspace)

    console.print(f"\n{_display_logo()} zen-claw is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.zen-claw/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print('  2. Chat: [cyan]zen-claw agent -m "Hello!"[/cyan]')
    console.print(
        "\n[dim]Want Telegram/WhatsApp? See: https://github.com/ZachCharles666/zen-claw/[/dim]"
    )


def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
        "SOUL.md": """# Soul

I am zen-claw, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
    }

    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")

    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


def _make_provider(config, model: str | None = None):
    """Create LiteLLMProvider from config. Exits if no API key found."""
    from zen_claw.providers.litellm_provider import LiteLLMProvider

    effective_model = str(model or config.agents.defaults.model or "").strip()
    p = config.get_provider(effective_model)
    if not (p and p.api_key) and not effective_model.startswith("bedrock/"):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.zen-claw/config.json under providers section")
        raise typer.Exit(1)
    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(effective_model),
        default_model=effective_model,
        extra_headers=p.extra_headers if p else None,
        rate_limit_delay_sec=p.rate_limit_delay_sec if p else 0.0,
    )


def _create_sidecar_supervisor(config):
    """Create sidecar supervisor when enabled in config."""
    if not bool(config.tools.sidecar_supervisor):
        return None
    from zen_claw.runtime.sidecar_supervisor import SidecarSupervisor

    return SidecarSupervisor(config)


def _print_effective_tool_backends(config) -> None:
    """Print non-sensitive effective backend config for quick operational checks."""
    exec_cfg = config.tools.effective_exec()
    search_cfg = config.tools.effective_search()
    fetch_cfg = config.tools.effective_fetch()
    browser_cfg = config.tools.effective_browser()
    hardening = config.tools.policy.production_hardening

    console.print("[cyan]Effective Tool Backends[/cyan]")
    console.print(f"  productionHardening: {hardening}")
    if hardening:
        console.print("  hardeningRules: noLegacyConfig, noLocalFallback, subagentGuardrailLocked")
    console.print(
        f"  exec: mode={exec_cfg.mode}, healthcheck={exec_cfg.sidecar_healthcheck}, "
        f"fallbackToLocal={exec_cfg.sidecar_fallback_to_local}"
    )
    console.print(
        f"  web_search: mode={search_cfg.mode}, healthcheck={search_cfg.proxy_healthcheck}, "
        f"fallbackToLocal={search_cfg.proxy_fallback_to_local}"
    )
    console.print(
        f"  web_fetch: mode={fetch_cfg.mode}, healthcheck={fetch_cfg.proxy_healthcheck}, "
        f"fallbackToLocal={fetch_cfg.proxy_fallback_to_local}"
    )
    console.print(
        f"  browser: mode={browser_cfg.mode}, healthcheck={browser_cfg.sidecar_healthcheck}, "
        f"fallbackToOff={browser_cfg.sidecar_fallback_to_off}, maxSteps={browser_cfg.max_steps}"
    )
    guardrail_on = not config.tools.policy.allow_subagent_sensitive_tools
    kill_switch_on = bool(config.tools.policy.kill_switch_enabled)
    kill_switch_reason = (config.tools.policy.kill_switch_reason or "").strip()
    reason_text = f", reason={kill_switch_reason}" if kill_switch_reason else ""
    console.print(f"  globalKillSwitch: {kill_switch_on}{reason_text}")
    console.print(f"  subagentHardGuardrail: {guardrail_on}")
    cron_channels = config.tools.policy.cron_allowed_channels
    console.print(
        "  cronAllowedChannels: " + (", ".join(cron_channels) if cron_channels else "(all)")
    )
    cron_actions = config.tools.policy.cron_allowed_actions_by_channel
    if cron_actions:
        parts = [f"{k}={'/'.join(v)}" for k, v in sorted(cron_actions.items())]
        console.print("  cronAllowedActionsByChannel: " + ", ".join(parts))
    else:
        console.print("  cronAllowedActionsByChannel: (all actions)")
    console.print(
        "  cronRequireRemoveConfirmation: "
        + str(config.tools.policy.cron_require_remove_confirmation)
    )
    channel_scopes = sorted(config.tools.policy.channel_policies.keys())
    console.print(
        "  channelPolicyScopes: " + (", ".join(channel_scopes) if channel_scopes else "(none)")
    )
    console.print("  skillPermissionsMode: " + str(config.agents.defaults.skill_permissions_mode))
    if not guardrail_on:
        console.print(
            "[yellow]Warning:[/yellow] subagent hard guardrail is disabled "
            "(allowSubagentSensitiveTools=true). This is high risk."
        )


def _print_sidecar_status(config) -> None:
    """Print sidecar runtime status based on current config and state files."""
    from zen_claw.runtime.sidecar_supervisor import collect_sidecar_status

    rows = collect_sidecar_status(config)
    if not rows:
        return

    console.print("[cyan]Sidecar Status[/cyan]")
    for row in rows:
        console.print(
            "  "
            + f"{row['name']}: status={row['status']}, "
            + f"managed={row['managed']}, "
            + f"pid={row['pid'] if row['pid'] is not None else '-'}, "
            + f"uptime={row['uptime']}, "
            + f"health={row['health']}"
        )
    console.print(
        "  source: tools.network (canonical); legacy tools.exec/tools.web.* are compatibility inputs"
    )


def _print_channel_rate_limit_status(config) -> None:
    """Print channel rate-limit config and runtime counters."""
    import json

    from zen_claw.config.loader import get_data_dir

    console.print("[cyan]Channel Rate Limit[/cyan]")
    console.print(
        "  default: "
        + f"mode={config.channels.outbound_rate_limit_mode}, "
        + f"rate={config.channels.outbound_rate_limit_per_sec}/s, "
        + f"burst={config.channels.outbound_rate_limit_burst}"
    )
    overrides = config.channels.outbound_rate_limit_by_channel
    if overrides:
        for name, cfg in sorted(overrides.items()):
            mode = cfg.mode if cfg.mode is not None else config.channels.outbound_rate_limit_mode
            per_sec = (
                cfg.per_sec
                if cfg.per_sec is not None
                else config.channels.outbound_rate_limit_per_sec
            )
            burst = (
                cfg.burst if cfg.burst is not None else config.channels.outbound_rate_limit_burst
            )
            console.print(f"  override.{name}: mode={mode}, rate={per_sec}/s, burst={burst}")
    else:
        console.print("  overrides: (none)")

    stats_file = get_data_dir() / "channels" / "rate_limit_stats.json"
    if not stats_file.exists():
        console.print("  runtime: (no stats yet)")
        return
    try:
        stats = json.loads(stats_file.read_text(encoding="utf-8"))
        channels = stats.get("channels", {}) if isinstance(stats, dict) else {}
        if not channels:
            console.print("  runtime: (no stats yet)")
            return
        for name, row in sorted(channels.items()):
            console.print(
                "  runtime."
                + f"{name}: delayed={int(row.get('delayed_count', 0))}, "
                + f"dropped={int(row.get('dropped_count', 0))}, "
                + f"lastDelayMs={int(row.get('last_delay_ms', 0))}"
            )
    except (OSError, ValueError):
        console.print("  runtime: (stats unreadable)")


def _print_channel_rbac_status(config, verbose: bool = False) -> None:
    """Print per-channel RBAC status summary for operational visibility."""
    from zen_claw.channels.registry import build_channel_rbac_row, iter_channel_specs

    console.print("[cyan]Channel RBAC[/cyan]")
    for spec in iter_channel_specs():
        row = build_channel_rbac_row(config, spec)
        console.print(
            f"  {row['name']}: enabled={row['rbac_enabled']}, admins={row['admins']}, users={row['users']}"
        )
        if verbose:
            admins = row["admins_list"]
            users = row["users_list"]
            console.print("    admin_ids: " + (", ".join(admins) if admins else "(none)"))
            console.print("    user_ids: " + (", ".join(users) if users else "(none)"))


def _print_agent_profile_status(config, verbose: bool = False) -> None:
    """Print agent profile isolation summary."""
    from zen_claw.agent.pool import list_agent_profiles

    profiles = list_agent_profiles(config)
    console.print("[cyan]Agent Profiles[/cyan]")
    for profile in profiles:
        prompt_bound = bool(profile.system_prompt or profile.system_prompt_file)
        tools_bound = bool(profile.allowed_tools is not None or profile.denied_tools is not None)
        console.print(
            "  "
            + f"{profile.agent_id}: registered={profile.is_registered}, "
            + f"model={profile.model}, "
            + f"workspace={profile.workspace}, "
            + f"promptBound={prompt_bound}, "
            + f"toolBound={tools_bound}"
        )
        if verbose:
            console.print("    memoryIsolation: workspace-scoped")
            console.print("    sessionIsolation: workspace-scoped")
            console.print("    skillsIsolation: workspace-scoped")
            prompt_file = profile.system_prompt_file or "(none)"
            console.print(
                "    promptBinding: "
                + (
                    f"inline={'yes' if bool(profile.system_prompt) else 'no'}, file={prompt_file}"
                )
            )
            allow = ", ".join(profile.allowed_tools or []) if profile.allowed_tools else "(none)"
            deny = ", ".join(profile.denied_tools or []) if profile.denied_tools else "(none)"
            console.print(f"    toolBinding: allow={allow}, deny={deny}")


def _print_node_token_rotation_status(within_sec: int = 3600) -> None:
    """Print node token rotation hygiene summary."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.node.service import NodeService

    svc = NodeService(get_data_dir() / "nodes" / "state.json")
    result = svc.scan_token_rotation(within_sec=within_sec, rotate=False)
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []

    reason_count: dict[str, int] = {"revoked": 0, "expired": 0, "expiring_soon": 0, "no_expiry": 0}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip().lower()
        if reason in reason_count:
            reason_count[reason] += 1

    checked = int(result.get("checked") or 0)
    total_candidates = len([x for x in candidates if isinstance(x, dict)])
    console.print("[cyan]Node Token Rotation[/cyan]")
    console.print(
        "  "
        + f"checked={checked}, candidates={total_candidates}, "
        + f"revoked={reason_count['revoked']}, "
        + f"expired={reason_count['expired']}, "
        + f"expiringSoon={reason_count['expiring_soon']}, "
        + f"noExpiry={reason_count['no_expiry']}, "
        + f"windowSec={within_sec}"
    )


def _print_policy_audit_matrix(config) -> None:
    """Print matrix-style policy audit for role/policy/skill-scope governance."""
    from zen_claw.agent.skills import SkillsLoader

    def _fmt(values: list[str] | None) -> str:
        if values is None:
            return "(unset)"
        if not values:
            return "(none)"
        return ", ".join(values)

    console.print("[cyan]Policy Audit Matrix[/cyan]")
    console.print("  role.admin -> allow=*, deny=(none)")
    console.print("  role.user -> allow=read_file, web_search, deny=(all others)")
    console.print(
        "  policy.agent -> allow="
        + _fmt(config.tools.policy.agent.allow)
        + ", deny="
        + _fmt(config.tools.policy.agent.deny)
    )
    console.print(
        "  policy.subagent -> allow="
        + _fmt(config.tools.policy.subagent.allow)
        + ", deny="
        + _fmt(config.tools.policy.subagent.deny)
    )

    if config.tools.policy.channel_policies:
        for ch, layer in sorted(config.tools.policy.channel_policies.items()):
            console.print(
                f"  policy.channel.{ch} -> allow={_fmt(layer.allow)}, deny={_fmt(layer.deny)}"
            )
    else:
        console.print("  policy.channel.* -> (none)")

    loader = SkillsLoader(config.workspace_path)
    rows = loader.validate_all_skill_manifests(strict=False)
    if not rows:
        console.print("  skills.scopes -> (no skills found)")
        return

    for row in rows:
        name = str(row.get("name") or "")
        ok = bool(row.get("ok"))
        manifest, errs = loader.get_skill_manifest(name)
        if errs:
            console.print(f"  skills.{name} -> manifest=missing")
            continue
        scopes = manifest.get("scopes") if isinstance(manifest, dict) else None
        perms = manifest.get("permissions") if isinstance(manifest, dict) else None
        scope_text = ", ".join(scopes) if isinstance(scopes, list) and scopes else "(none)"
        perm_text = ", ".join(perms) if isinstance(perms, list) and perms else "(none)"
        status = "valid" if ok else "invalid"
        console.print(
            f"  skills.{name} -> manifest={status}, scopes={scope_text}, permissions={perm_text}"
        )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Dashboard bind host"),
    port: int = typer.Option(18791, "--port", "-p", help="Dashboard port"),
    refresh_sec: int = typer.Option(5, "--refresh-sec", help="Auto-refresh interval in seconds"),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Allow non-localhost bind (security risk)"
    ),
):
    """Start read-only local dashboard."""
    from zen_claw.config.loader import load_config
    from zen_claw.dashboard.server import run_dashboard_server

    host_norm = host.strip().lower()
    is_local = host_norm in {"127.0.0.1", "localhost", "::1"}
    if not is_local and not allow_remote:
        console.print(
            "[red]Refused:[/red] non-localhost dashboard bind is blocked by default. "
            "Use [cyan]--allow-remote[/cyan] only in trusted networks."
        )
        raise typer.Exit(1)

    config = load_config()
    refresh = max(1, int(refresh_sec))
    console.print(f"{_display_logo()} Starting dashboard at http://{host}:{port} (refresh={refresh}s)")
    try:
        run_dashboard_server(config, host=host, port=port, refresh_sec=refresh)
    except KeyboardInterrupt:
        console.print("\nDashboard stopped.")


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    node_chaos_enabled: bool = typer.Option(
        False,
        "--node-chaos-enabled",
        help="Enable node dispatcher chaos injection (drill mode)",
    ),
    node_chaos_fail_every: int = typer.Option(
        0,
        "--node-chaos-fail-every",
        help="Inject one outbound failure for every N sends per channel (0=off)",
    ),
    node_chaos_channel: list[str] = typer.Option(
        [],
        "--node-chaos-channel",
        help="Target channel for chaos injection (repeatable, '*' means all)",
    ),
):
    """Start the zen-claw gateway."""
    from zen_claw.agent.loop import AgentLoop
    from zen_claw.agent.pool import AgentPool
    from zen_claw.agent.router import AgentRouter
    from zen_claw.bus.queue import MessageBus
    from zen_claw.channels.manager import ChannelManager
    from zen_claw.config.loader import get_data_dir, load_config
    from zen_claw.cron.service import CronService
    from zen_claw.cron.types import CronJob
    from zen_claw.heartbeat.service import HeartbeatService
    from zen_claw.node.dispatcher import NodeTaskDispatcher
    from zen_claw.node.service import NodeService

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{_display_logo()} Starting zen-claw gateway on port {port}...")
    config = load_config()
    bus = MessageBus()
    provider = _make_provider(config)
    _print_effective_tool_backends(config)
    sidecar_supervisor = _create_sidecar_supervisor(config)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    exec_cfg = config.tools.effective_exec()
    search_cfg = config.tools.effective_search()
    fetch_cfg = config.tools.effective_fetch()
    browser_cfg = config.tools.effective_browser()
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        vision_model=config.agents.defaults.vision_model or None,
        memory_recall_mode=config.agents.defaults.memory_recall_mode,
        enable_planning=config.agents.defaults.enable_planning,
        max_reflections=config.agents.defaults.max_reflections,
        auto_parameter_rewrite=config.agents.defaults.auto_parameter_rewrite,
        max_context_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=search_cfg.api_key or None,
        web_search_config=search_cfg,
        web_fetch_config=fetch_cfg,
        browser_config=browser_cfg,
        exec_config=exec_cfg,
        tool_policy_config=config.tools.policy,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        compression_trigger_ratio=config.agents.defaults.compression_trigger_ratio,
        compression_hysteresis_ratio=config.agents.defaults.compression_hysteresis_ratio,
        compression_cooldown_turns=config.agents.defaults.compression_cooldown_turns,
        thinking_model=config.agents.defaults.thinking_model or None,
        fallback_model=config.agents.defaults.fallback_model or None,
        intent_model_overrides=config.agents.defaults.intent_model_overrides,
    )
    agent_pool = AgentPool(
        config=config,
        bus=bus,
        provider=provider,
        provider_factory=lambda model=None: _make_provider(config, model=model),
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        if _cron_job_has_crawler_ingest(job):
            return await _execute_crawler_cron_job(job, data_dir=get_data_dir())
        if _cron_job_has_knowledge_ingest(job):
            return await _execute_knowledge_cron_job(job, data_dir=get_data_dir())

        target_url = (
            job.payload.target_url or config.channels.webhook_trigger.cron_target_url or ""
        ).strip()
        if target_url:
            import httpx

            body = {
                "content": job.payload.message,
                "chat_id": job.payload.to or "",
                "channel": job.payload.channel or "",
                "job_id": job.id,
            }
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if config.channels.webhook_trigger.secret:
                ts = str(int(time.time()))
                nonce = secrets.token_hex(8)
                payload = f"{ts}.{nonce}.".encode("utf-8") + body_bytes
                sig = hmac.new(
                    config.channels.webhook_trigger.secret.encode("utf-8"),
                    payload,
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Timestamp"] = ts
                headers["X-Nonce"] = nonce
                headers["X-Signature"] = sig
            for k, v in (job.payload.target_headers or {}).items():
                headers[str(k)] = str(v)
            timeout_sec = max(
                1,
                int(
                    job.payload.target_timeout_sec
                    or config.channels.webhook_trigger.cron_target_timeout_sec
                    or 10
                ),
            )
            async with httpx.AsyncClient(timeout=float(timeout_sec)) as client:
                method = (job.payload.target_method or "POST").upper()
                resp = await client.request(method, target_url, content=body_bytes, headers=headers)
                resp.raise_for_status()
            return "cron webhook trigger sent"

        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            from zen_claw.bus.events import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )
        return response

    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(prompt, session_key="heartbeat")

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True,
    )

    # Create channel manager before routing callbacks so gateway can reuse sticky routes.
    channels = ChannelManager(config, bus)
    health_server = _start_gateway_health_server(
        port=port,
        webhook_trigger_channel=channels.get_channel("webhook_trigger"),
    )
    console.print(f"[green]✓[/green] Gateway health: http://127.0.0.1:{port}/api/v1/health")
    agent_router = AgentRouter(
        config,
        resolve_bound_agent=lambda channel, chat_id, user_id: channels.resolve_agent(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
        ),
    )

    # Bridge node task queue to gateway+agent execution loop.
    node_service = NodeService(get_data_dir() / "nodes" / "state.json")

    async def on_node_agent_prompt(
        prompt: str,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        media: list[str] | None = None,
        agent_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        decision = _resolve_routed_agent(
            agent_router,
            channel=channel,
            chat_id=chat_id,
            user_id=chat_id,
            content=prompt,
            explicit_agent_id=agent_id or "",
        )
        target_agent_id = decision.agent_id
        target_loop = (
            agent
            if target_agent_id == "default"
            else await agent_pool.get_or_create(target_agent_id)
        )
        scoped_session_key = (
            session_key
            if target_agent_id == "default"
            else f"agent:{target_agent_id}:{session_key}"
        )
        return await target_loop.process_direct(
            prompt,
            session_key=scoped_session_key,
            channel=channel,
            chat_id=chat_id,
            media=media,
            trace_id=trace_id,
        )

    node_dispatcher = NodeTaskDispatcher(
        node_service=node_service,
        on_agent_prompt=on_node_agent_prompt,
        publish_outbound=bus.publish_outbound,
        poll_interval_sec=2.0,
        enabled=True,
        chaos_enabled=node_chaos_enabled,
        chaos_fail_every=node_chaos_fail_every,
        chaos_channels=node_chaos_channel,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")
    console.print("[green]✓[/green] Node dispatcher: enabled (poll=2s)")
    if node_chaos_enabled and node_chaos_fail_every > 0:
        targets = ", ".join(node_chaos_channel) if node_chaos_channel else "*"
        console.print(
            "[yellow]Warning:[/yellow] Node chaos drill enabled: "
            + f"failEvery={node_chaos_fail_every}, channels={targets}"
        )

    async def run():
        async def run_inbound_router() -> None:
            while True:
                try:
                    msg = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
                    await _process_inbound_with_agent_router(
                        msg,
                        router=agent_router,
                        default_agent=agent,
                        agent_pool=agent_pool,
                        bus=bus,
                        channel_manager=channels,
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

        try:
            if sidecar_supervisor:
                await sidecar_supervisor.start()
            await cron.start()
            await heartbeat.start()
            await node_dispatcher.start()
            await asyncio.gather(
                run_inbound_router(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            await node_dispatcher.stop()
            agent.stop()
            await channels.stop_all()
        finally:
            await node_dispatcher.stop()
            if sidecar_supervisor:
                await sidecar_supervisor.stop()

    try:
        asyncio.run(run())
    finally:
        health_server.shutdown()
        health_server.server_close()


# ============================================================================
# Agent Commands
# ============================================================================

agent_app = typer.Typer(
    help="Agent orchestration commands",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(agent_app, name="agent")


def _run_agent_cli(
    *,
    message: str | None,
    session_id: str,
    agent_profile: str,
    skill: list[str],
    media: list[str],
    skill_perms: str,
) -> None:
    from zen_claw.agent.loop import AgentLoop
    from zen_claw.agent.pool import resolve_agent_profile
    from zen_claw.bus.queue import MessageBus
    from zen_claw.config.loader import load_config

    config = load_config()
    profile = resolve_agent_profile(config, agent_profile or "default")
    _print_effective_tool_backends(config)
    sidecar_supervisor = _create_sidecar_supervisor(config)

    bus = MessageBus()
    provider = _make_provider(config, model=profile.model)
    exec_cfg = config.tools.effective_exec()
    search_cfg = config.tools.effective_search()
    fetch_cfg = config.tools.effective_fetch()
    browser_cfg = config.tools.effective_browser()

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=profile.workspace,
        model=profile.model,
        system_prompt_override=profile.system_prompt or None,
        system_prompt_file=profile.system_prompt_file or None,
        profile_allowed_tools=profile.allowed_tools,
        profile_denied_tools=profile.denied_tools,
        vision_model=profile.vision_model or None,
        memory_recall_mode=profile.memory_recall_mode,
        enable_planning=profile.enable_planning,
        max_reflections=profile.max_reflections,
        auto_parameter_rewrite=profile.auto_parameter_rewrite,
        max_context_tokens=profile.max_tokens,
        max_iterations=profile.max_tool_iterations,
        brave_api_key=search_cfg.api_key or None,
        web_search_config=search_cfg,
        web_fetch_config=fetch_cfg,
        browser_config=browser_cfg,
        exec_config=exec_cfg,
        tool_policy_config=config.tools.policy,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        compression_trigger_ratio=profile.compression_trigger_ratio,
        compression_hysteresis_ratio=profile.compression_hysteresis_ratio,
        compression_cooldown_turns=profile.compression_cooldown_turns,
        thinking_model=profile.thinking_model or None,
        fallback_model=profile.fallback_model or None,
        intent_model_overrides=profile.intent_model_overrides,
        skill_names=(list(profile.skill_names) + [name for name in skill if name not in profile.skill_names]),
        skill_permissions_mode=skill_perms or profile.skill_permissions_mode,
        allowed_models=profile.allowed_models,
    )
    agent_loop.workspace.mkdir(parents=True, exist_ok=True)

    if profile.agent_id != "default":
        console.print(
            "Agent Profile: "
            + f"{profile.agent_id}"
            + (" (registered)" if profile.is_registered else " (ad-hoc)")
        )

    if skill:
        console.print(
            "Skill Slot: "
            + (
                ", ".join(list(profile.skill_names) + [name for name in skill if name not in profile.skill_names])
                if (profile.skill_names or skill)
                else "(none)"
            )
            + f" (skillPermsEffectiveThisRun={agent_loop.skill_permissions_mode})"
        )
    elif profile.skill_names:
        console.print(
            "Skill Slot: "
            + ", ".join(profile.skill_names)
            + f" (skillPermsEffectiveThisRun={agent_loop.skill_permissions_mode})"
        )

    if message:
        async def run_once():
            try:
                if sidecar_supervisor:
                    await sidecar_supervisor.start()
                response = await agent_loop.process_direct(
                    message,
                    session_id,
                    media=media or None,
                )
                console.print(f"\n{_display_logo()} {response}", soft_wrap=True)
            finally:
                if sidecar_supervisor:
                    await sidecar_supervisor.stop()

        asyncio.run(run_once())
        return

    console.print(f"{_display_logo()} Interactive mode (Ctrl+C to exit)\n")

    async def run_interactive():
        try:
            if sidecar_supervisor:
                await sidecar_supervisor.start()
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await agent_loop.process_direct(
                        user_input,
                        session_id,
                        media=media or None,
                    )
                    console.print(f"\n{_display_logo()} {response}\n", soft_wrap=True)
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break
        finally:
            if sidecar_supervisor:
                await sidecar_supervisor.stop()

    asyncio.run(run_interactive())


@agent_app.callback()
def agent(
    ctx: typer.Context,
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
    agent_profile: str = typer.Option(
        "",
        "--agent-profile",
        help="Run against a registered agent profile or ad-hoc agent id",
    ),
    skill: list[str] = typer.Option(
        [], "--skill", help="Load skill(s) fully into the system prompt (repeatable)"
    ),
    media: list[str] = typer.Option(
        [], "--media", help="Attach media refs/paths for this run (repeatable)"
    ),
    skill_perms: str = typer.Option(
        "",
        "--skill-perms",
        help="Skill permission gate for requested skills: off|warn|enforce (default from config)",
    ),
):
    """Interact with the agent directly, or use subcommands like `list/chat/test`."""
    if ctx.invoked_subcommand is not None:
        return
    _run_agent_cli(
        message=message,
        session_id=session_id,
        agent_profile=agent_profile,
        skill=skill,
        media=media,
        skill_perms=skill_perms,
    )


def _cron_job_has_crawler_ingest(job: Any) -> bool:
    return bool(str(getattr(job.payload, "crawler_source_url", "") or "").strip())


@agent_app.command("list")
def agent_list_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List registered agent profiles."""
    from zen_claw.agent.pool import list_agent_profiles
    from zen_claw.config.loader import load_config

    config = load_config()
    profiles = list_agent_profiles(config)
    if json_output:
        console.print_json(
            data=[
                {
                    "agent_id": profile.agent_id,
                    "display_name": profile.display_name,
                    "description": profile.description,
                    "workspace": str(profile.workspace),
                    "model": profile.model,
                    "enable_planning": profile.enable_planning,
                    "skill_names": list(profile.skill_names),
                    "registered": profile.is_registered,
                }
                for profile in profiles
            ]
        )
        return

    table = Table(title="Agent Profiles")
    table.add_column("ID")
    table.add_column("Display Name")
    table.add_column("Model")
    table.add_column("Planning")
    table.add_column("Skills")
    table.add_column("Workspace")
    for profile in profiles:
        table.add_row(
            profile.agent_id,
            profile.display_name,
            profile.model,
            "on" if profile.enable_planning else "off",
            ", ".join(profile.skill_names) if profile.skill_names else "-",
            str(profile.workspace),
        )
    console.print(table)


@app.command("agent-list")
def agent_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Compatibility alias for `zen-claw agent list`."""
    agent_list_command(json_output=json_output)


@agent_app.command("chat")
def agent_chat(
    profile_id: str = typer.Argument(..., help="Registered agent profile or ad-hoc agent id"),
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
    skill: list[str] = typer.Option(
        [], "--skill", help="Load skill(s) fully into the system prompt (repeatable)"
    ),
    media: list[str] = typer.Option(
        [], "--media", help="Attach media refs/paths for this run (repeatable)"
    ),
    skill_perms: str = typer.Option(
        "",
        "--skill-perms",
        help="Skill permission gate for requested skills: off|warn|enforce (default from config)",
    ),
):
    """Chat with a specific agent profile."""
    _run_agent_cli(
        message=message,
        session_id=session_id,
        agent_profile=profile_id,
        skill=skill,
        media=media,
        skill_perms=skill_perms,
    )


@agent_app.command("test")
def agent_test(
    profile_id: str = typer.Argument(..., help="Registered agent profile or ad-hoc agent id"),
    prompt: str = typer.Option("ping", "--prompt", "-p", help="Smoke-test prompt"),
    session_id: str = typer.Option("cli:agent-test", "--session", "-s", help="Session ID"),
):
    """Run a minimal smoke test against a specific agent profile."""
    _run_agent_cli(
        message=prompt,
        session_id=session_id,
        agent_profile=profile_id,
        skill=[],
        media=[],
        skill_perms="",
    )


# ============================================================================
# Config Commands
# ============================================================================

config_app = typer.Typer(help="Manage configuration")
app.add_typer(config_app, name="config")


def _provider_catalog() -> list[dict[str, str]]:
    """Provider matrix for config UX and quick capability audit."""
    return [
        {
            "id": "openrouter",
            "label": "OpenRouter",
            "coverage": "global",
            "models": "Anthropic/OpenAI/Gemini/DeepSeek/Mistral/Llama",
            "path": "providers.openrouter",
        },
        {
            "id": "anthropic",
            "label": "Anthropic",
            "coverage": "global",
            "models": "Claude series",
            "path": "providers.anthropic",
        },
        {
            "id": "openai",
            "label": "OpenAI",
            "coverage": "global",
            "models": "GPT series",
            "path": "providers.openai",
        },
        {
            "id": "gemini",
            "label": "Google Gemini",
            "coverage": "global",
            "models": "Gemini series",
            "path": "providers.gemini",
        },
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "coverage": "china/global",
            "models": "DeepSeek chat/reasoner",
            "path": "providers.deepseek",
        },
        {
            "id": "zhipu",
            "label": "Zhipu (GLM)",
            "coverage": "china",
            "models": "GLM/ZAI series",
            "path": "providers.zhipu",
        },
        {
            "id": "dashscope",
            "label": "Alibaba DashScope",
            "coverage": "china",
            "models": "Qwen/Tongyi series",
            "path": "providers.dashscope",
        },
        {
            "id": "moonshot",
            "label": "Moonshot",
            "coverage": "china",
            "models": "Kimi/Moonshot series",
            "path": "providers.moonshot",
        },
        {
            "id": "groq",
            "label": "Groq",
            "coverage": "global",
            "models": "Llama/other hosted models",
            "path": "providers.groq",
        },
        {
            "id": "aihubmix",
            "label": "AiHubMix (Gateway)",
            "coverage": "china/global",
            "models": "OpenAI-compatible gateway",
            "path": "providers.aihubmix",
        },
        {
            "id": "vllm",
            "label": "vLLM / self-hosted",
            "coverage": "self-hosted",
            "models": "OpenAI-compatible local/remote endpoint",
            "path": "providers.vllm",
        },
    ]


def _config_template_catalog() -> dict[str, dict[str, str]]:
    return {
        "global-openrouter": {
            "provider": "openrouter",
            "api_base": "https://openrouter.ai/api/v1",
            "model": "openrouter/anthropic/claude-3.5-sonnet",
        },
        "global-openai": {
            "provider": "openai",
            "api_base": "",
            "model": "openai/gpt-4o-mini",
        },
        "global-anthropic": {
            "provider": "anthropic",
            "api_base": "",
            "model": "anthropic/claude-3-5-sonnet-20241022",
        },
        "global-gemini": {
            "provider": "gemini",
            "api_base": "",
            "model": "gemini/gemini-1.5-pro",
        },
        "china-deepseek": {
            "provider": "deepseek",
            "api_base": "",
            "model": "deepseek/deepseek-chat",
        },
        "china-zhipu": {
            "provider": "zhipu",
            "api_base": "",
            "model": "zhipu/glm-4-plus",
        },
        "china-dashscope": {
            "provider": "dashscope",
            "api_base": "",
            "model": "dashscope/qwen-plus",
        },
        "china-moonshot": {
            "provider": "moonshot",
            "api_base": "",
            "model": "moonshot/moonshot-v1-8k",
        },
    }


def _wizard_profile_catalog() -> list[dict[str, str]]:
    return [
        {
            "id": "global-openrouter",
            "label": "Global gateway (recommended)",
            "summary": "OpenRouter with a ready-to-use gateway model prefix",
            "provider": "openrouter",
        },
        {
            "id": "china-deepseek",
            "label": "China mainstream",
            "summary": "Direct DeepSeek setup for a simple mainland-first path",
            "provider": "deepseek",
        },
        {
            "id": "custom-openai-compatible",
            "label": "Custom OpenAI-compatible",
            "summary": "Self-hosted or third-party OpenAI-compatible endpoint",
            "provider": "vllm",
        },
    ]


def _default_api_base_for_provider(provider: str) -> str:
    return {
        "openrouter": "https://openrouter.ai/api/v1",
        "aihubmix": "https://aihubmix.com/v1",
    }.get(str(provider or "").strip().lower(), "")


def _provider_recommended_models(provider: str) -> list[str]:
    rows = {
        "openrouter": [
            "openrouter/anthropic/claude-3.5-sonnet",
            "openrouter/deepseek/deepseek-chat",
            "openrouter/stepfun/step-3.5-flash:free",
        ],
        "openai": [
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
        ],
        "anthropic": [
            "anthropic/claude-3-5-sonnet-20241022",
            "anthropic/claude-opus-4-5",
        ],
        "gemini": ["gemini/gemini-1.5-pro"],
        "deepseek": ["deepseek/deepseek-chat"],
        "zhipu": ["zhipu/glm-4-plus"],
        "dashscope": ["dashscope/qwen-plus"],
        "moonshot": ["moonshot/moonshot-v1-8k"],
    }
    return rows.get(str(provider or "").strip().lower(), [])


def _normalize_wizard_profile(profile: str) -> str:
    raw = str(profile or "").strip().lower()
    aliases = {
        "openrouter": "global-openrouter",
        "global": "global-openrouter",
        "deepseek": "china-deepseek",
        "china": "china-deepseek",
        "vllm": "custom-openai-compatible",
        "custom": "custom-openai-compatible",
        "openai-compatible": "custom-openai-compatible",
    }
    return aliases.get(raw, raw)


def _looks_like_duplicated_secret(value: str) -> bool:
    token = str(value or "").strip()
    if len(token) < 20 or len(token) % 2 != 0:
        return False
    half = len(token) // 2
    return token[:half] == token[half:]


def _infer_provider_id(model_name: str) -> str:
    m = (model_name or "").lower()
    if "openrouter" in m:
        return "openrouter"
    if "aihubmix" in m:
        return "aihubmix"
    if "anthropic" in m or "claude" in m:
        return "anthropic"
    if "openai" in m or "gpt" in m:
        return "openai"
    if "gemini" in m:
        return "gemini"
    if "deepseek" in m:
        return "deepseek"
    if "zhipu" in m or "glm" in m or "zai" in m:
        return "zhipu"
    if "dashscope" in m or "qwen" in m:
        return "dashscope"
    if "moonshot" in m or "kimi" in m:
        return "moonshot"
    if "groq" in m:
        return "groq"
    if "vllm" in m or "hosted_vllm/" in m:
        return "vllm"
    return ""


def _provider_id_from_config(config_obj, provider_obj) -> str:
    for row in _provider_catalog():
        pid = row["id"]
        if getattr(config_obj.providers, pid) is provider_obj:
            return pid
    return ""


def _describe_key_pattern(provider: str, api_key: str) -> str:
    key = str(api_key or "").strip()
    pid = str(provider or "").strip().lower()
    if not key:
        return "empty"
    if pid == "openrouter":
        return "looks like OpenRouter key" if key.startswith("sk-or-") else "prefix does not look like OpenRouter"
    if pid == "anthropic":
        return "looks like Anthropic key" if key.startswith("sk-ant-") else "prefix not recognized for Anthropic"
    if pid == "openai":
        return "looks like OpenAI key" if key.startswith("sk-") else "prefix not recognized for OpenAI"
    return "no provider-specific key check"


def _provider_alignment_issues(
    *,
    model: str,
    selected_provider: str,
    api_base: str,
    api_key: str,
    fallback_word: str,
) -> list[tuple[str, str, str]]:
    issues: list[tuple[str, str, str]] = []
    model = str(model or "").strip()
    provider = str(selected_provider or "").strip().lower()
    base = str(api_base or "").strip()
    inferred_id = _infer_provider_id(model)

    if not model:
        issues.append(
            (
                "FAIL",
                "agents.defaults.model is empty.",
                "Set a model in provider/model form, for example `openrouter/anthropic/claude-3.5-sonnet`.",
            )
        )
    elif inferred_id and provider and inferred_id != provider:
        issues.append(
            (
                "WARN",
                f"Model suggests `{inferred_id}` but provider selection currently resolves to `{provider}`.",
                "Keep provider/model/apiBase on the same route, or switch back to a recommended model for the selected provider.",
            )
        )
    elif provider and not inferred_id:
        issues.append(
            (
                "WARN",
                f"Model prefix is not recognized, so provider selection currently {fallback_word} `{provider}`.",
                "Use an explicit `provider/model` prefix to avoid provider fallback ambiguity.",
            )
        )

    if provider == "vllm" and not base:
        issues.append(
            (
                "FAIL",
                "vLLM / self-hosted setup requires an explicit apiBase.",
                "Set providers.vllm.apiBase to your OpenAI-compatible endpoint.",
            )
        )

    if provider in {"openrouter", "aihubmix"} and not base:
        issues.append(
            (
                "WARN",
                f"`{provider}` apiBase is empty; the built-in gateway default will be used.",
                "Keep the default if intentional, or set apiBase explicitly for a private proxy.",
            )
        )

    if provider == "openrouter" and base and "openrouter" not in base.lower():
        issues.append(
            (
                "WARN",
                "openrouter selected but apiBase does not look like OpenRouter.",
                "Check providers.openrouter.apiBase.",
            )
        )
    if provider == "aihubmix" and base and "aihubmix" not in base.lower():
        issues.append(
            (
                "WARN",
                "aihubmix selected but apiBase does not look like AiHubMix.",
                "Check providers.aihubmix.apiBase.",
            )
        )

    if provider in {"openrouter", "aihubmix"} and _looks_like_duplicated_secret(api_key):
        issues.append(
            (
                "FAIL",
                f"`providers.{provider}.apiKey` appears to contain the same secret twice.",
                "Paste the API key once only, then rerun the wizard.",
            )
        )

    if provider and not str(api_key or "").strip():
        issues.append(
            (
                "FAIL",
                f"`providers.{provider}.apiKey` is empty.",
                "Set the provider key in `zen-claw config wizard` before running `agent -m`.",
            )
        )

    return issues


def _troubleshooting_hints(
    *,
    model_name: str,
    inferred_pid: str,
    matched_pid: str,
    key_ids: list[str],
    config_path: Path,
    duplicated_key_providers: list[str],
) -> list[str]:
    hints: list[str] = [f"当前读取的配置文件是 `{config_path}`。该路径与当前工作目录无关。"]
    if not key_ids:
        hints.append(
            "未配置任何 API Key：先运行 `zen-claw config wizard`，再执行 `zen-claw config doctor --strict`。"
        )
    if model_name and not inferred_pid:
        hints.append(
            "模型前缀未识别：建议使用 `provider/model` 形式（例如 `openrouter/...` 或 `deepseek/...`）。"
        )
    if inferred_pid and matched_pid and inferred_pid != matched_pid:
        hints.append(
            f"模型前缀与实际 provider 不一致：当前模型更像 `{inferred_pid}`，但解析到 `{matched_pid}`。请检查 model/apiKey/apiBase 三者是否同源。"
        )
    if duplicated_key_providers:
        hints.append(
            "检测到疑似重复粘贴的 API Key：该 key 看起来像把同一个 secret 粘贴了两次。"
        )
    if inferred_pid in {"openrouter", "aihubmix"} and inferred_pid in key_ids:
        hints.append("网关类 provider 建议显式设置 apiBase，避免在多环境切换时指向错误。")
    if inferred_pid == "vllm":
        hints.append(
            "vLLM 场景务必配置 `providers.vllm.apiBase` 指向 OpenAI-compatible endpoint。"
        )
    if len(hints) == 1:
        hints.append(
            "未发现典型配置冲突。若调用仍失败，请先执行 `zen-claw status -v` 查看实际生效后端。"
        )
    return hints


def _build_config_template(profile: str) -> dict[str, object] | None:
    catalog = _config_template_catalog()
    row = catalog.get(profile)
    if not row:
        return None
    provider = row["provider"]
    model = row["model"]
    api_base = row["api_base"]
    providers: dict[str, dict[str, str]] = {provider: {"apiKey": "<API_KEY>"}}
    if api_base:
        providers[provider]["apiBase"] = api_base
    return {
        "agents": {"defaults": {"model": model}},
        "providers": providers,
        "meta": {
            "profile": profile,
            "generatedBy": "zen-claw config template",
        },
    }


@config_app.command("providers")
def config_providers(
    json_out: bool = typer.Option(False, "--json", help="Output JSON format"),
):
    """Show supported provider coverage matrix for config planning."""
    rows = _provider_catalog()
    if json_out:
        console.print_json(data=rows)
        return

    table = Table(title="LLM Provider Coverage")
    table.add_column("Provider", style="cyan")
    table.add_column("Coverage")
    table.add_column("Mainstream Models")
    table.add_column("Config Path")
    for row in rows:
        table.add_row(row["label"], row["coverage"], row["models"], row["path"])
    console.print(table)

    global_count = len([r for r in rows if r["coverage"] in {"global", "china/global"}])
    china_count = len([r for r in rows if r["coverage"] in {"china", "china/global"}])
    console.print(
        f"\nSummary: global-capable={global_count}, china-mainstream-capable={china_count}"
    )


@config_app.command("template")
def config_template(
    profile: str = typer.Option(
        "global-openrouter",
        "--profile",
        help=(
            "Template profile: global-openrouter|global-openai|global-anthropic|global-gemini|"
            "china-deepseek|china-zhipu|china-dashscope|china-moonshot"
        ),
    ),
    out: str = typer.Option("", "--out", "-o", help="Write template JSON to file"),
    show_wizard: bool = typer.Option(
        False, "--show-wizard", help="Print equivalent config wizard command"
    ),
):
    """Generate executable provider config template snippets."""
    import json

    profile_key = str(profile or "").strip().lower()
    payload = _build_config_template(profile_key)
    if payload is None:
        valid = ", ".join(sorted(_config_template_catalog().keys()))
        console.print(f"[red]Unsupported profile:[/red] {profile_key}")
        console.print(f"Valid profiles: {valid}")
        raise typer.Exit(1)

    if out.strip():
        out_path = Path(out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[green]✓[/green] template written: {out_path}")
    else:
        console.print_json(data=payload)

    if show_wizard:
        row = _config_template_catalog()[profile_key]
        provider = row["provider"]
        model = row["model"]
        api_base = row["api_base"]
        cmd = f"zen-claw config wizard --provider {provider} --api-key <API_KEY> --model {model}"
        if api_base:
            cmd += f" --api-base {api_base}"
        cmd += " -y"
        console.print("\nEquivalent wizard command:")
        console.print(f"  {cmd}")


@config_app.command("wizard")
def config_wizard(
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Config file path (default: ~/.zen-claw/config.json)",
    ),
    provider: str = typer.Option(
        "",
        "--provider",
        help="Provider id (e.g. openrouter/openai/anthropic/gemini/zhipu/dashscope/deepseek/moonshot/groq/aihubmix/vllm)",
    ),
    api_key: str = typer.Option("", "--api-key", help="Provider API key"),
    model: str = typer.Option("", "--model", help="Default model for agents.defaults.model"),
    api_base: str = typer.Option("", "--api-base", help="Optional provider API base override"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without confirmation prompt"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview changes only; do not write file"
    ),
):
    """Step-by-step provider setup wizard (interactive or scripted)."""
    from zen_claw.config.loader import get_config_path, load_config, save_config

    path = config_path or get_config_path()
    config = load_config(path)
    catalog = _provider_catalog()
    valid_ids = [row["id"] for row in catalog]
    selected_profile = ""
    provider_was_explicit = bool(provider.strip())
    model_was_explicit = bool(model.strip())
    api_base_was_explicit = bool(api_base.strip())
    profile_default_model = ""
    profile_default_base = ""

    if not provider_was_explicit:
        profile_rows = _wizard_profile_catalog()
        profile_table = Table(title="Config Wizard - Step 1/4: Choose setup profile")
        profile_table.add_column("Profile", style="cyan")
        profile_table.add_column("Default provider")
        profile_table.add_column("When to use")
        for row in profile_rows:
            profile_table.add_row(row["id"], row["provider"], row["summary"])
        console.print(profile_table)
        selected_profile = _normalize_wizard_profile(
            typer.prompt("Setup profile", default="global-openrouter")
        )
        profile_row = next((row for row in profile_rows if row["id"] == selected_profile), None)
        if profile_row is None:
            console.print(f"[red]Unsupported setup profile:[/red] {selected_profile}")
            console.print(
                "Valid profiles: " + ", ".join(sorted(row["id"] for row in profile_rows))
            )
            raise typer.Exit(1)
        template_defaults = _config_template_catalog().get(selected_profile, {})
        provider = str(profile_row["provider"])
        profile_default_model = str(template_defaults.get("model", "") or "")
        profile_default_base = str(template_defaults.get("api_base", "") or "")

        table = Table(title="Config Wizard - Step 2/4: Confirm provider")
        table.add_column("ID", style="cyan")
        table.add_column("Provider")
        table.add_column("Coverage")
        table.add_column("Mainstream Models")
        for row in catalog:
            table.add_row(row["id"], row["label"], row["coverage"], row["models"])
        console.print(table)
        provider = typer.prompt("Provider ID", default=provider)

    provider = provider.strip().lower()

    if provider not in valid_ids:
        console.print(f"[red]Unsupported provider:[/red] {provider}")
        console.print("Use [cyan]zen-claw config providers[/cyan] to list available provider ids.")
        raise typer.Exit(1)

    provider_cfg = getattr(config.providers, provider)
    current_key = str(provider_cfg.api_key or "")
    current_model = str(config.agents.defaults.model or "")
    current_base = str(provider_cfg.api_base or _default_api_base_for_provider(provider) or "")

    if not model_was_explicit:
        recommended_models = _provider_recommended_models(provider)
        effective_default_model = ""
        if profile_default_model:
            effective_default_model = profile_default_model
        elif current_model and (
            _infer_provider_id(current_model) == provider
            or current_model.lower().startswith(f"{provider}/")
        ):
            effective_default_model = current_model
        elif recommended_models:
            effective_default_model = recommended_models[0]
        if recommended_models:
            table = Table(title="Config Wizard - Step 3/4: Choose model")
            table.add_column("Recommended model", style="cyan")
            for recommended in recommended_models:
                table.add_row(recommended)
            console.print(table)
        model = typer.prompt("Default model", default=effective_default_model)
    if not api_base_was_explicit:
        effective_default_base = profile_default_base or current_base
        if effective_default_base:
            console.print(
                f"Default API base for `{provider}`: {effective_default_base}"
            )
        api_base = typer.prompt(
            "API base (leave blank to use provider default)",
            default=effective_default_base,
        )
    if not api_key:
        if current_key:
            console.print(
                f"Existing API key detected for `{provider}`. Leave blank to keep the current key."
            )
        console.print("Config Wizard - Step 4/4: API key")
        api_key = (
            typer.prompt("API key", default="", hide_input=True, show_default=False).strip()
            or current_key
        )

    alignment_issues = _provider_alignment_issues(
        model=model,
        selected_provider=provider,
        api_base=api_base,
        api_key=api_key,
        fallback_word="falls back to",
    )
    fail_issues = [issue for issue in alignment_issues if issue[0] == "FAIL"]
    warn_issues = [issue for issue in alignment_issues if issue[0] == "WARN"]

    key_pattern = _describe_key_pattern(provider, api_key)
    key_length = len(str(api_key or "").strip())
    key_duplicate = "yes" if _looks_like_duplicated_secret(api_key) else "no"
    if current_key and api_key == current_key:
        key_source = "kept existing"
    elif api_key:
        key_source = "new input"
    else:
        key_source = "empty"

    summary = Table(title="Config Wizard Summary")
    summary.add_column("Field", style="cyan")
    summary.add_column("Value", overflow="fold")
    summary.add_row("config_path", str(path))
    if selected_profile:
        summary.add_row("setup_profile", selected_profile)
    summary.add_row("provider", provider)
    summary.add_row("default_model", str(model or "").strip() or "(empty)")
    summary.add_row("api_base", api_base.strip() or "(default/none)")
    summary.add_row("api_key", "set" if bool(str(api_key or "").strip()) else "empty")
    summary.add_row("key_source", key_source)
    summary.add_row("key_length", str(key_length))
    summary.add_row("key_pattern", key_pattern)
    summary.add_row("key_duplicate_check", key_duplicate)
    summary.add_row("written", "no (dry-run)" if dry_run else "pending")
    console.print(summary)

    if alignment_issues:
        issue_table = Table(title="Config Wizard Preflight")
        issue_table.add_column("Level", style="cyan")
        issue_table.add_column("Check")
        issue_table.add_column("Guidance", overflow="fold")
        for level, msg, fix in alignment_issues:
            style = {"FAIL": "red", "WARN": "yellow"}.get(level, "white")
            issue_table.add_row(f"[{style}]{level}[/{style}]", msg, fix)
        console.print(issue_table)

    if fail_issues:
        raise typer.Exit(1)
    if warn_issues and yes and not dry_run:
        console.print(
            "[red]Refusing to write config with unresolved warnings in non-interactive --yes mode.[/red]"
        )
        console.print("Re-run without `--yes` to review and confirm the warnings interactively.")
        raise typer.Exit(1)
    if warn_issues and not dry_run:
        if not typer.confirm("Continue with these warnings?"):
            console.print("Cancelled.")
            raise typer.Exit(1)

    # Apply non-empty values only.
    if api_key.strip():
        provider_cfg.api_key = api_key.strip()
    config.agents.defaults.model = model.strip()
    provider_cfg.api_base = api_base.strip() or None

    if dry_run:
        console.print("[yellow]Dry-run:[/yellow] configuration preview only; no file write.")
    else:
        if not yes and not typer.confirm(f"Write changes to {path}?"):
            console.print("Cancelled.")
            raise typer.Exit(0)
        save_config(config, path)
        console.print(f"[green]Config written:[/green] {path}")
    console.print(
        '\nNext: [cyan]zen-claw config doctor --strict[/cyan] then [cyan]zen-claw agent -m "Hello"[/cyan]'
    )


@config_app.command("doctor")
def config_doctor(
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Config file path (default: ~/.zen-claw/config.json)",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as failures (non-zero exit).",
    ),
):
    """Validate provider/model/api_base consistency and show fix guidance."""
    from zen_claw.config.loader import get_config_path, load_config

    path = config_path or get_config_path()
    config = load_config(path)
    model = str(config.agents.defaults.model or "")
    inferred_id = _infer_provider_id(model)
    matched_provider = config.get_provider(model)
    matched_id = _provider_id_from_config(config, matched_provider) if matched_provider else ""

    issues: list[tuple[str, str, str]] = []
    infos: list[str] = []

    key_enabled = []
    for row in _provider_catalog():
        pid = row["id"]
        if str(getattr(config.providers, pid).api_key or "").strip():
            key_enabled.append(pid)
    duplicated_key_providers = [
        pid
        for pid in ("openrouter", "aihubmix")
        if _looks_like_duplicated_secret(str(getattr(config.providers, pid).api_key or "").strip())
    ]
    selected_cfg = getattr(config.providers, matched_id) if matched_id else None
    infos.append("providers_with_key=" + (", ".join(key_enabled) if key_enabled else "(none)"))
    infos.append("config_path=" + str(path))
    infos.append("default_model=" + (model or "(empty)"))
    infos.append("inferred_provider=" + (inferred_id or "(unknown)"))
    infos.append("selected_provider=" + (matched_id or "(none)"))

    if not key_enabled:
        issues.append(
            (
                "FAIL",
                "No provider API key configured.",
                "Run `zen-claw config wizard` to set one provider key.",
            )
        )

    issues.extend(
        _provider_alignment_issues(
            model=model,
            selected_provider=matched_id,
            api_base=str(getattr(selected_cfg, "api_base", "") or ""),
            api_key=str(getattr(selected_cfg, "api_key", "") or ""),
            fallback_word="falls back to",
        )
    )

    if inferred_id:
        inferred_cfg = getattr(config.providers, inferred_id)
        if not str(inferred_cfg.api_key or "").strip():
            issues.append(
                (
                    "WARN",
                    f"Inferred provider `{inferred_id}` has no apiKey.",
                    f"Set `providers.{inferred_id}.apiKey` or change model prefix.",
                )
            )

    browser_mode = str(getattr(config.tools.network.browser, "mode", "") or "").strip().lower()
    net_proxy_allow = str(os.environ.get("NET_PROXY_ALLOW_DOMAINS", "") or "").strip()
    if browser_mode == "sidecar" and not net_proxy_allow:
        issues.append(
            (
                "WARN",
                "Browser sidecar is enabled but no domain allowlist is configured.",
                (
                    "Set NET_PROXY_ALLOW_DOMAINS or tools.network.browser.allowedDomains "
                    "to avoid unrestricted domain navigation."
                ),
            )
        )

    # Agent compression guardrails (P3-B).
    trigger = float(config.agents.defaults.compression_trigger_ratio)
    hysteresis = float(config.agents.defaults.compression_hysteresis_ratio)
    cooldown = int(config.agents.defaults.compression_cooldown_turns)
    if not (0.0 < trigger < 1.0):
        issues.append(
            (
                "WARN",
                "agents.defaults.compressionTriggerRatio should be between 0 and 1.",
                "Use a value like 0.8.",
            )
        )
    if not (0.0 < hysteresis < 1.0):
        issues.append(
            (
                "WARN",
                "agents.defaults.compressionHysteresisRatio should be between 0 and 1.",
                "Use a value like 0.5.",
            )
        )
    if hysteresis > trigger:
        issues.append(
            (
                "WARN",
                "compressionHysteresisRatio is larger than compressionTriggerRatio.",
                "Set hysteresis <= trigger to avoid unstable compression toggling.",
            )
        )
    if cooldown < 0:
        issues.append(
            (
                "WARN",
                "agents.defaults.compressionCooldownTurns should be >= 0.",
                "Use an integer like 5.",
            )
        )

    # Multi-agent routing hygiene (P3-A).
    channel_profiles = {
        "telegram": config.channels.telegram.agent_profile,
        "discord": config.channels.discord.agent_profile,
        "whatsapp": config.channels.whatsapp.agent_profile,
        "feishu": config.channels.feishu.agent_profile,
        "wechat_mp": config.channels.wechat_mp.agent_profile,
        "wecom": config.channels.wecom.agent_profile,
        "dingtalk": config.channels.dingtalk.agent_profile,
        "webchat": config.channels.webchat.agent_profile,
        "webhook_trigger": config.channels.webhook_trigger.agent_profile,
        "slack": config.channels.slack.agent_profile,
        "signal": config.channels.signal.agent_profile,
        "matrix": config.channels.matrix.agent_profile,
    }
    known_profiles = {"default", *config.agents.profiles.keys()}
    for channel_name, profile in channel_profiles.items():
        p = str(profile or "").strip()
        if not p:
            issues.append(
                (
                    "WARN",
                    f"channels.{channel_name}.agentProfile is empty.",
                    "Set a non-empty agent profile (e.g. `default`).",
                )
            )
            continue
        if any(ch.isspace() for ch in p):
            issues.append(
                (
                    "WARN",
                    f"channels.{channel_name}.agentProfile contains whitespace.",
                    "Use simple IDs like `default`, `alpha`, `assistant_a`.",
                )
            )
            continue
        if p.lower() not in known_profiles:
            issues.append(
                (
                    "WARN",
                    f"channels.{channel_name}.agentProfile references unknown profile `{p}`.",
                    "Register it under agents.profiles or switch the channel back to `default`.",
                )
            )

    for profile_id, profile_cfg in sorted(config.agents.profiles.items()):
        workspace_root = (
            Path(str(profile_cfg.workspace).strip()).expanduser().resolve()
            if str(profile_cfg.workspace).strip()
            else None
        )
        prompt_file = str(profile_cfg.system_prompt_file or "").strip()
        if prompt_file:
            prompt_path = Path(prompt_file).expanduser()
            if not prompt_path.is_absolute():
                if workspace_root is None:
                    issues.append(
                        (
                            "WARN",
                            f"agents.profiles.{profile_id}.systemPromptFile is relative but profile workspace is unset.",
                            "Set agents.profiles.<id>.workspace or use an absolute prompt path inside the intended workspace.",
                        )
                    )
                else:
                    prompt_path = (workspace_root / prompt_path).resolve()
            else:
                prompt_path = prompt_path.resolve()
            if workspace_root is not None:
                try:
                    prompt_path.relative_to(workspace_root)
                except ValueError:
                    issues.append(
                        (
                            "WARN",
                            f"agents.profiles.{profile_id}.systemPromptFile points outside its workspace.",
                            "Keep profile prompt files inside the profile workspace for reproducible isolation.",
                        )
                    )
            if not prompt_path.exists():
                issues.append(
                    (
                        "WARN",
                        f"agents.profiles.{profile_id}.systemPromptFile does not exist.",
                        "Create the prompt file or clear systemPromptFile.",
                    )
                )
        overlap = sorted(
            set(profile_cfg.allowed_tools or []).intersection(set(profile_cfg.denied_tools or []))
        )
        if overlap:
            issues.append(
                (
                    "WARN",
                    f"agents.profiles.{profile_id} overlaps allowedTools/deniedTools: {', '.join(overlap)}.",
                    "Remove overlapping tool names so the profile contract is unambiguous.",
                )
            )
        infos.append(
            "agent_profile."
            + f"{profile_id}: workspace={'set' if workspace_root is not None else 'derived'}, "
            + f"prompt_bound={bool(str(profile_cfg.system_prompt or '').strip() or prompt_file)}, "
            + f"tool_bound={bool(profile_cfg.allowed_tools is not None or profile_cfg.denied_tools is not None)}"
        )

    # Channel-specific checks for newly added schema fields/channels.
    if config.channels.webchat.enabled and not str(config.channels.webchat.token or "").strip():
        issues.append(
            (
                "WARN",
                "webchat is enabled but channels.webchat.token is empty.",
                "Set a token to avoid exposing dashboard chat websocket publicly.",
            )
        )

    webhook_cfg = config.channels.webhook_trigger
    if webhook_cfg.enabled:
        has_secret = bool(str(webhook_cfg.secret or "").strip())
        has_api_key = bool(str(webhook_cfg.api_key or "").strip())
        has_ip_allowlist = bool(webhook_cfg.ip_allowlist)
        if not (has_secret or has_api_key or has_ip_allowlist):
            issues.append(
                (
                    "FAIL",
                    "webhook_trigger is enabled but no auth path is configured.",
                    "Set secret/apiKey or configure ipAllowlist for controlled unsigned access.",
                )
            )
        if int(webhook_cfg.timestamp_tolerance_sec) <= 0:
            issues.append(
                (
                    "FAIL",
                    "webhook_trigger.timestampToleranceSec must be > 0.",
                    "Use a value like 300.",
                )
            )
        if int(webhook_cfg.nonce_ttl_sec) <= 0:
            issues.append(
                (
                    "FAIL",
                    "webhook_trigger.nonceTtlSec must be > 0.",
                    "Use a value like 600.",
                )
            )
        if int(webhook_cfg.nonce_ttl_sec) < int(webhook_cfg.timestamp_tolerance_sec):
            issues.append(
                (
                    "WARN",
                    "webhook_trigger.nonceTtlSec is smaller than timestampToleranceSec.",
                    "Set nonce TTL >= timestamp tolerance to reduce replay-window edge cases.",
                )
            )

    slack_cfg = config.channels.slack
    if slack_cfg.enabled:
        if not str(slack_cfg.bot_token or "").strip():
            issues.append(
                (
                    "FAIL",
                    "slack is enabled but channels.slack.botToken is empty.",
                    "Set botToken or disable Slack channel.",
                )
            )
        if slack_cfg.socket_mode and not str(slack_cfg.app_token or "").strip():
            issues.append(
                (
                    "WARN",
                    "slack socketMode is enabled but appToken is empty.",
                    "Set channels.slack.appToken for Socket Mode, or disable socketMode.",
                )
            )
        if (not slack_cfg.socket_mode) and not str(slack_cfg.signing_secret or "").strip():
            issues.append(
                (
                    "WARN",
                    "slack HTTP callback mode enabled but signingSecret is empty.",
                    "Set channels.slack.signingSecret to verify callback signatures.",
                )
            )

    signal_cfg = config.channels.signal
    if signal_cfg.enabled:
        if signal_cfg.mode == "signal_cli" and not str(signal_cfg.account or "").strip():
            issues.append(
                (
                    "FAIL",
                    "signal mode=signal_cli requires channels.signal.account.",
                    "Set channels.signal.account (phone number) or switch to signald mode.",
                )
            )
        if signal_cfg.mode == "signald" and not str(signal_cfg.account or "").strip():
            issues.append(
                (
                    "WARN",
                    "signal mode=signald without account disables inbound receive loop.",
                    "Set channels.signal.account for inbound message polling.",
                )
            )

    matrix_cfg = config.channels.matrix
    if matrix_cfg.enabled:
        if not str(matrix_cfg.homeserver or "").strip():
            issues.append(
                (
                    "FAIL",
                    "matrix is enabled but channels.matrix.homeserver is empty.",
                    "Set homeserver like https://matrix.org.",
                )
            )
        has_matrix_token = bool(str(matrix_cfg.access_token or "").strip())
        has_matrix_credentials = bool(str(matrix_cfg.username or "").strip()) and bool(
            str(matrix_cfg.password or "")
        )
        can_auto_auth = has_matrix_credentials and (
            bool(matrix_cfg.auto_login) or bool(matrix_cfg.auto_register)
        )
        if not has_matrix_token and not can_auto_auth:
            issues.append(
                (
                    "FAIL",
                    "matrix is enabled but no usable auth path is configured.",
                    "Set accessToken, or set username/password with autoLogin or autoRegister.",
                )
            )
        if matrix_cfg.e2ee_require and not matrix_cfg.e2ee_enabled:
            issues.append(
                (
                    "FAIL",
                    "matrix.e2eeRequire=true requires matrix.e2eeEnabled=true.",
                    "Enable e2eeEnabled or disable e2eeRequire.",
                )
            )

    table = Table(title="Config Doctor")
    table.add_column("Level", style="cyan")
    table.add_column("Check")
    table.add_column("Guidance", overflow="fold")
    for level, msg, fix in issues:
        style = {"FAIL": "red", "WARN": "yellow"}.get(level, "white")
        table.add_row(f"[{style}]{level}[/{style}]", msg, fix)
    if not issues:
        table.add_row(
            "[green]PASS[/green]",
            "No configuration issues detected.",
            'You can run `zen-claw agent -m "Hello"`.',
        )
    console.print(table)

    console.print("\n[cyan]Context[/cyan]")
    for info in infos:
        console.print(f"  - {info}")

    has_fail = any(level == "FAIL" for level, _, _ in issues)
    has_warn = any(level == "WARN" for level, _, _ in issues)

    console.print("\n[cyan]Troubleshooting[/cyan]")
    for hint in _troubleshooting_hints(
        model_name=model,
        inferred_pid=inferred_id,
        matched_pid=matched_id,
        key_ids=key_enabled,
        config_path=path,
        duplicated_key_providers=duplicated_key_providers,
    ):
        console.print(f"  - {hint}")

    console.print("\n[cyan]Config Self-check Commands[/cyan]")
    console.print("  - zen-claw config providers")
    console.print("  - zen-claw config wizard --dry-run")
    console.print("  - zen-claw config doctor --strict")
    console.print("  - zen-claw status -v")

    console.print("\n[cyan]Production Suggestions[/cyan]")
    console.print("  - 优先使用统一网关出口，避免多 provider 混用导致行为漂移。")
    console.print("  - 设定密钥轮换周期并用最小权限 key。")
    console.print("  - 按环境拆分配置（dev/staging/prod），避免共用同一份 config。")

    if has_fail or (strict and has_warn):
        raise typer.Exit(1)


@config_app.command("troubleshoot")
def config_troubleshoot(
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Config file path (default: ~/.zen-claw/config.json)",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as failures (non-zero exit).",
    ),
):
    """Targeted troubleshooting for model prefix/apiBase/provider mismatch issues."""
    from zen_claw.config.loader import get_config_path, load_config

    path = config_path or get_config_path()
    config = load_config(path)
    model = str(config.agents.defaults.model or "").strip()
    model_lower = model.lower()

    known_prefixes = [
        "openrouter/",
        "openai/",
        "anthropic/",
        "gemini/",
        "deepseek/",
        "zhipu/",
        "dashscope/",
        "moonshot/",
        "groq/",
        "aihubmix/",
        "vllm/",
    ]
    prefix_ok = any(model_lower.startswith(p) for p in known_prefixes)

    provider_by_model = ""
    if "openrouter" in model_lower:
        provider_by_model = "openrouter"
    elif "openai" in model_lower or "gpt" in model_lower:
        provider_by_model = "openai"
    elif "anthropic" in model_lower or "claude" in model_lower:
        provider_by_model = "anthropic"
    elif "gemini" in model_lower:
        provider_by_model = "gemini"
    elif "deepseek" in model_lower:
        provider_by_model = "deepseek"
    elif "zhipu" in model_lower or "glm" in model_lower or "zai" in model_lower:
        provider_by_model = "zhipu"
    elif "dashscope" in model_lower or "qwen" in model_lower:
        provider_by_model = "dashscope"
    elif "moonshot" in model_lower or "kimi" in model_lower:
        provider_by_model = "moonshot"
    elif "groq" in model_lower:
        provider_by_model = "groq"
    elif "aihubmix" in model_lower:
        provider_by_model = "aihubmix"
    elif "vllm" in model_lower:
        provider_by_model = "vllm"

    selected_provider_obj = config.get_provider(model)
    selected_provider = ""
    for row in _provider_catalog():
        pid = row["id"]
        if getattr(config.providers, pid) is selected_provider_obj:
            selected_provider = pid
            break

    issues = _provider_alignment_issues(
        model=model,
        selected_provider=selected_provider,
        api_base=str(getattr(config.providers, selected_provider).api_base or "") if selected_provider else "",
        api_key=str(getattr(config.providers, selected_provider).api_key or "") if selected_provider else "",
        fallback_word="falls back to",
    )
    if not prefix_ok and not any("Model prefix is not recognized" in msg for _, msg, _ in issues):
        issues.append(
            (
                "WARN",
                "模型名前缀未识别（建议 provider/model 风格）",
                "示例：openrouter/anthropic/claude-3.5-sonnet",
            )
        )
    if provider_by_model and selected_provider and provider_by_model != selected_provider:
        issues.append(
            (
                "WARN",
                f"provider 选择与默认模型不匹配：model->{provider_by_model}, selected->{selected_provider}",
                "检查 model 前缀、对应 provider 的 apiKey/apiBase 是否同源",
            )
        )

    table = Table(title="Config Troubleshoot")
    table.add_column("Level", style="cyan")
    table.add_column("Issue")
    table.add_column("Suggestion", overflow="fold")
    if not issues:
        table.add_row(
            "[green]PASS[/green]",
            "未发现常见配置冲突",
            "可继续执行 `zen-claw config doctor --strict`",
        )
    else:
        for level, issue, suggestion in issues:
            style = "red" if level == "FAIL" else "yellow"
            table.add_row(f"[{style}]{level}[/{style}]", issue, suggestion)
    console.print(table)
    console.print(f"\nConfig source: {path}")

    has_fail = any(level == "FAIL" for level, _, _ in issues)
    has_warn = any(level == "WARN" for level, _, _ in issues)
    if has_fail or (strict and has_warn):
        raise typer.Exit(1)


@config_app.command("production-check")
def config_production_check(
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Config file path (default: ~/.zen-claw/config.json)",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as failures (non-zero exit).",
    ),
):
    """Production-oriented config checks for gateway egress, rotation policy and env isolation."""
    import os

    from zen_claw.config.loader import get_config_path, load_config

    path = (config_path or get_config_path()).resolve()
    config = load_config(path)

    gateway_ids = {"openrouter", "aihubmix"}
    direct_ids = {
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "zhipu",
        "dashscope",
        "moonshot",
        "groq",
    }

    enabled_keys: set[str] = set()
    for row in _provider_catalog():
        pid = row["id"]
        api_key = str(getattr(config.providers, pid).api_key or "").strip()
        if api_key:
            enabled_keys.add(pid)

    vllm_base = str(config.providers.vllm.api_base or "").strip()
    gateway_enabled = sorted([p for p in enabled_keys if p in gateway_ids])
    direct_enabled = sorted([p for p in enabled_keys if p in direct_ids])

    checks: list[tuple[str, str, str]] = []

    if not enabled_keys and not vllm_base:
        checks.append(
            (
                "FAIL",
                "No production egress provider is configured",
                "Configure one gateway/provider key via `zen-claw config wizard`",
            )
        )
    elif gateway_enabled and not direct_enabled:
        checks.append(
            (
                "PASS",
                f"Unified gateway egress: {', '.join(gateway_enabled)}",
                "Keep direct provider keys disabled in production",
            )
        )
    elif gateway_enabled and direct_enabled:
        checks.append(
            (
                "WARN",
                f"Mixed egress detected: gateway={gateway_enabled}, direct={direct_enabled}",
                "Prefer a unified gateway egress to reduce drift",
            )
        )
    else:
        checks.append(
            (
                "WARN",
                f"Direct provider egress only: {direct_enabled}",
                "Prefer OpenRouter/AiHubMix gateway for unified governance",
            )
        )

    rot_days_raw = str(os.environ.get("zen_claw_KEY_ROTATION_DAYS", "")).strip()
    if not rot_days_raw:
        checks.append(
            (
                "WARN",
                "Key rotation policy env `zen_claw_KEY_ROTATION_DAYS` is not set",
                "Set a rotation window (recommended <= 90 days)",
            )
        )
    else:
        try:
            rot_days = int(rot_days_raw)
            if rot_days <= 0:
                checks.append(
                    (
                        "FAIL",
                        "Invalid key rotation policy value",
                        "Set `zen_claw_KEY_ROTATION_DAYS` to a positive integer",
                    )
                )
            elif rot_days > 90:
                checks.append(
                    (
                        "WARN",
                        f"Key rotation window is too long: {rot_days} days",
                        "Use <= 90 days for production keys",
                    )
                )
            else:
                checks.append(
                    (
                        "PASS",
                        f"Key rotation policy set: {rot_days} days",
                        "Review and enforce least-privilege keys per environment",
                    )
                )
        except ValueError:
            checks.append(
                (
                    "FAIL",
                    f"Invalid key rotation policy value: {rot_days_raw}",
                    "Set `zen_claw_KEY_ROTATION_DAYS` to a positive integer",
                )
            )

    env_name = str(os.environ.get("zen_claw_ENV", "")).strip().lower()
    if not env_name:
        checks.append(
            (
                "WARN",
                "Runtime env tag `zen_claw_ENV` is not set",
                "Set `zen_claw_ENV` (dev/staging/prod) and keep separate config files",
            )
        )
    else:
        path_text = str(path).lower()
        if env_name in path_text:
            checks.append(
                (
                    "PASS",
                    f"Config path appears environment-scoped: {path.name}",
                    "Keep dev/staging/prod config separated",
                )
            )
        else:
            checks.append(
                (
                    "WARN",
                    f"Config path may not be environment-scoped for env={env_name}",
                    "Use env-specific config path naming (e.g. config.prod.json)",
                )
            )

    table = Table(title="Config Production Check")
    table.add_column("Level", style="cyan")
    table.add_column("Check")
    table.add_column("Guidance", overflow="fold")
    for level, check, guide in checks:
        style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(level, "white")
        table.add_row(f"[{style}]{level}[/{style}]", check, guide)
    console.print(table)

    has_fail = any(level == "FAIL" for level, _, _ in checks)
    has_warn = any(level == "WARN" for level, _, _ in checks)
    if has_fail or (strict and has_warn):
        raise typer.Exit(1)


@config_app.command("migrate")
def config_migrate(
    config_path: Optional[Path] = typer.Option(
        None, "--config", help="Config file path (default: ~/.zen-claw/config.json)"
    ),
    write: bool = typer.Option(
        False, "--write", help="Write migrated config to disk (default is dry-run)"
    ),
    out_path: Optional[Path] = typer.Option(
        None, "--out", help="Write migrated config to a new file"
    ),
    backup: bool = typer.Option(
        True, "--backup", help="When writing in-place, create .bak copy first"
    ),
    no_backup: bool = typer.Option(
        False, "--no-backup", help="Disable .bak copy when writing in-place"
    ),
):
    """Migrate legacy config fields to current schema."""
    import json
    from copy import deepcopy

    from zen_claw.config.loader import (
        _migrate_config,
        convert_keys,
        get_config_path,
        save_config,
    )
    from zen_claw.config.schema import Config

    def _diff_paths(before, after, base: str = "") -> list[str]:
        paths: list[str] = []
        if isinstance(before, dict) and isinstance(after, dict):
            keys = sorted(set(before.keys()) | set(after.keys()))
            for key in keys:
                nxt = f"{base}.{key}" if base else str(key)
                if key not in before or key not in after:
                    paths.append(nxt)
                    continue
                paths.extend(_diff_paths(before[key], after[key], nxt))
            return paths
        if before != after:
            paths.append(base or "<root>")
        return paths

    path = config_path or get_config_path()
    if no_backup:
        backup = False
    if not path.exists():
        console.print(f"[red]Config file not found:[/red] {path}")
        raise typer.Exit(1)

    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, ValueError) as e:
        console.print(f"[red]Failed to read config:[/red] {e}")
        raise typer.Exit(1)

    migrated = _migrate_config(deepcopy(raw))
    changed = json.dumps(raw, sort_keys=True) != json.dumps(migrated, sort_keys=True)
    changed_paths = _diff_paths(raw, migrated)

    if not changed:
        console.print("[green]No migration changes needed.[/green]")
        return

    if changed_paths:
        console.print("[cyan]Changed paths:[/cyan]")
        for p in changed_paths[:20]:
            console.print(f"  - {p}")
        if len(changed_paths) > 20:
            console.print(f"  - ... and {len(changed_paths) - 20} more")

    if not write:
        console.print("[yellow]Migration preview (dry-run): changes detected.[/yellow]")
        console.print(
            "Use [cyan]zen-claw config migrate --write[/cyan] to persist migrated config."
        )
        return

    target_path = out_path or path
    if out_path is not None:
        target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if target_path == path and backup and path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            backup_path.write_text(raw_text, encoding="utf-8")
            console.print(f"[dim]Backup created:[/dim] {backup_path}")
        validated = Config.model_validate(convert_keys(migrated))
        save_config(validated, target_path)
    except Exception as e:
        console.print(f"[red]Migration failed:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[green]Config migrated successfully:[/green] {target_path}")


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


credentials_app = typer.Typer(help="Manage encrypted credentials")
app.add_typer(credentials_app, name="credentials")


@credentials_app.command("set")
def credentials_set(
    platform: str = typer.Argument(..., help="Platform name, e.g. github/twitter"),
    key: str = typer.Argument(..., help="Credential key name"),
    value: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="Credential value (will not be echoed)",
    ),
) -> None:
    from zen_claw.auth.credentials import CredentialVault

    vault = CredentialVault()
    try:
        vault.store(str(platform or "").strip().lower(), str(key or "").strip(), value)
    except Exception as e:
        typer.echo(f"[ERROR] Failed to store credential: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[OK] Credential stored: platform={platform.lower()!r}, key={key!r}")


@credentials_app.command("list")
def credentials_list(
    platform: str | None = typer.Argument(default=None, help="Optional platform name"),
) -> None:
    from zen_claw.auth.credentials import CredentialVault

    vault = CredentialVault()
    if platform:
        p = str(platform or "").strip().lower()
        keys = vault.list_keys(p)
        if not keys:
            typer.echo(f"No credentials found for platform: {p!r}")
            return
        typer.echo(f"Platform: {p}")
        for k in keys:
            typer.echo(f"  - {k}")
        return
    platforms = vault.list_platforms()
    if not platforms:
        typer.echo("No credentials stored.")
        return
    for p in platforms:
        typer.echo(f"{p}:")
        for k in vault.list_keys(p):
            typer.echo(f"  - {k}")


@credentials_app.command("delete")
def credentials_delete(
    platform: str = typer.Argument(..., help="Platform name"),
    key: str = typer.Argument(..., help="Credential key name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    from zen_claw.auth.credentials import CredentialVault

    p = str(platform or "").strip().lower()
    k = str(key or "").strip()
    if not yes:
        if not typer.confirm(f"Delete credential platform={p!r}, key={k!r}?"):
            typer.echo("Aborted.")
            return
    vault = CredentialVault()
    try:
        deleted = vault.delete(p, k)
    except Exception as e:
        typer.echo(f"[ERROR] Failed to delete credential: {e}", err=True)
        raise typer.Exit(code=1)
    if deleted:
        typer.echo(f"[OK] Deleted: platform={p!r}, key={k!r}")
    else:
        typer.echo(f"[WARN] Credential not found: platform={p!r}, key={k!r}")


social_app = typer.Typer(help="Autonomous social platform agent commands")
app.add_typer(social_app, name="social")


@social_app.command("run")
def social_run(
    platform: str = typer.Option("moltbook", "--platform", "-p", help="Platform identifier"),
    submolt: str = typer.Option("", "--submolt", "-s", help="Community/submolt"),
    interval: int = typer.Option(3600, "--interval", "-i", help="Seconds between cycles"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compose but do not post"),
    once: bool = typer.Option(False, "--once", help="Run one cycle then exit"),
    base_url: str = typer.Option("", "--base-url", help="Platform base URL"),
    credential_key: str = typer.Option("", "--credential-key", help="Vault key for auth token"),
) -> None:
    from zen_claw.agent.social_loop import SocialAgentLoop, SocialPlatformConfig
    from zen_claw.auth.credentials import CredentialVault
    from zen_claw.config.loader import load_config
    from zen_claw.providers.litellm_provider import LiteLLMProvider
    from zen_claw.utils.helpers import get_workspace_path

    cfg = load_config()
    social_cfg = cfg.agents.social
    workspace = get_workspace_path(cfg.agents.defaults.workspace)

    effective_base = str(base_url or social_cfg.platform_base_url or "").strip()
    effective_submolt = str(submolt or social_cfg.submolt or "").strip()
    effective_key = str(credential_key or social_cfg.credential_key or f"{platform}_token").strip()
    if not effective_base:
        console.print("[red]Error:[/red] --base-url or agents.social.platformBaseUrl is required")
        raise typer.Exit(code=1)

    vault = CredentialVault()
    token = vault.get(str(platform).strip().lower(), effective_key)
    if not token:
        token = os.environ.get("SOCIAL_AUTH_TOKEN", "").strip()
    if not token:
        console.print(
            f"[red]Error:[/red] credential not found for platform={platform!r}, key={effective_key!r}. "
            "Use `zen-claw credentials set` first."
        )
        raise typer.Exit(code=1)

    provider_cfg = cfg.get_provider(cfg.agents.defaults.model)
    provider = LiteLLMProvider(
        api_key=provider_cfg.api_key if provider_cfg else None,
        api_base=provider_cfg.api_base if provider_cfg else None,
        default_model=cfg.agents.defaults.model,
        extra_headers=provider_cfg.extra_headers if provider_cfg else None,
        rate_limit_delay_sec=provider_cfg.rate_limit_delay_sec if provider_cfg else 0.0,
    )
    loop = SocialAgentLoop(
        config=cfg,
        platform_config=SocialPlatformConfig(
            platform=str(platform).strip().lower(),
            base_url=effective_base,
            submolt=effective_submolt,
            auth_header=f"Bearer {token}",
            max_posts_per_cycle=max(1, int(social_cfg.max_posts_per_cycle)),
            dry_run=bool(dry_run or social_cfg.dry_run),
            system_prompt_override=str(social_cfg.system_prompt_override or ""),
        ),
        provider=provider,
        workspace=workspace,
        model=cfg.agents.defaults.model,
    )
    mode = "DRY RUN" if loop._pc.dry_run else "LIVE"
    console.print(
        f"[cyan]Starting social agent[/cyan] platform={platform} submolt={effective_submolt or '(default)'} mode={mode}"
    )
    if once:
        result = asyncio.get_event_loop().run_until_complete(loop.run_once())
        console.print(
            f"cycle done fetched={result.posts_fetched} filtered={result.posts_filtered} "
            f"composed={result.responses_composed} posted={result.responses_posted} errors={len(result.errors)}"
        )
        return
    try:
        asyncio.get_event_loop().run_until_complete(
            loop.run_forever(interval_sec=max(1, int(interval)))
        )
    except KeyboardInterrupt:
        console.print("[yellow]Social agent stopped.[/yellow]")


identity_app = typer.Typer(help="Agent ed25519 cryptographic identity")
app.add_typer(identity_app, name="identity")


def _get_identity_instance():
    from zen_claw.auth.identity import AgentIdentity
    from zen_claw.config.loader import load_config
    from zen_claw.utils.helpers import get_workspace_path

    cfg = load_config()
    workspace = get_workspace_path(cfg.agents.defaults.workspace)
    key_dir_str = str(getattr(cfg.agents.identity, "key_dir", "") or "").strip()
    key_dir = Path(key_dir_str).expanduser() if key_dir_str else workspace / ".agent_keys"
    identity = AgentIdentity(key_dir)
    identity.get_or_create_keypair()
    return identity


@identity_app.command("show")
def identity_show() -> None:
    from zen_claw.auth.identity import AgentIdentityError

    try:
        identity = _get_identity_instance()
    except AgentIdentityError as e:
        console.print(f"[red]Identity error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print("[bold cyan]Agent Public Key (ed25519)[/bold cyan]")
    console.print(f"  Public key : [green]{identity.public_key_hex()}[/green]")
    console.print(f"  Created at : {identity.created_at() or 'unknown'}")
    console.print("  Algorithm  : ed25519")


@identity_app.command("sign")
def identity_sign(message: str = typer.Argument(..., help="Message to sign")) -> None:
    import json

    from zen_claw.auth.identity import AgentIdentityError

    try:
        identity = _get_identity_instance()
        sig = identity.sign(message.encode("utf-8"))
    except AgentIdentityError as e:
        console.print(f"[red]Identity error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        json.dumps(
            {
                "message": message,
                "signature": sig,
                "public_key": identity.public_key_hex(),
                "algorithm": "ed25519",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@identity_app.command("verify")
def identity_verify(
    public_key: str = typer.Argument(..., help="Hex ed25519 public key"),
    message: str = typer.Argument(..., help="Original message"),
    signature: str = typer.Argument(..., help="Base64url signature"),
) -> None:
    from zen_claw.auth.identity import AgentIdentity

    ok = AgentIdentity.verify(public_key, message.encode("utf-8"), signature)
    if ok:
        console.print("[green]Signature valid.[/green]")
        return
    console.print("[red]Signature INVALID.[/red]")
    raise typer.Exit(code=1)


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from zen_claw.channels.registry import build_channel_rbac_row, iter_channel_specs
    from zen_claw.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("RBAC", style="magenta")
    table.add_column("Transport", style="magenta")
    table.add_column("Admins", style="blue")
    table.add_column("Users", style="blue")
    table.add_column("Configuration", style="yellow")

    for spec in iter_channel_specs():
        row = build_channel_rbac_row(config, spec)
        table.add_row(
            row["display_name"],
            "yes" if row["enabled"] else "no",
            "yes" if row["rbac_enabled"] else "no",
            str(row["transport"]),
            str(row["admins"]),
            str(row["users"]),
            str(row["configuration"]),
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    user_bridge = Path.home() / ".zen-claw" / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # zen-claw/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall zen-claw")
        raise typer.Exit(1)

    console.print(f"{_display_logo()} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        lockfile = user_bridge / "package-lock.json"
        if not lockfile.exists():
            console.print("[red]Bridge lockfile missing. Refusing unlocked npm install.[/red]")
            console.print(
                "Reinstall from a release or source tree that includes bridge/package-lock.json."
            )
            raise typer.Exit(1)

        console.print("  Installing dependencies...")
        subprocess.run(["npm", "ci"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    bridge_dir = _get_bridge_dir()

    console.print(f"{_display_logo()} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time

    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000)
            )
            next_run = next_time

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(
        None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"
    ),
    target_url: str = typer.Option(
        None, "--target-url", help="Webhook target URL for callback trigger"
    ),
    target_method: str = typer.Option("POST", "--target-method", help="HTTP method: POST or PUT"),
    knowledge_source: str = typer.Option(
        None, "--knowledge-source", help="Local file or directory to ingest on each run"
    ),
    knowledge_notebook: str = typer.Option(
        "default", "--knowledge-notebook", help="Notebook used for knowledge ingest jobs"
    ),
    knowledge_retention_max_documents: int = typer.Option(
        0, "--knowledge-retention-max-documents", help="Optional retention keep-most-recent limit"
    ),
    knowledge_retention_max_age_days: int = typer.Option(
        0, "--knowledge-retention-max-age-days", help="Optional retention max-age in days"
    ),
):
    """Add a scheduled job."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService
    from zen_claw.cron.types import CronSchedule

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime

        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
        target_url=target_url,
        target_method=target_method,
        knowledge_source=knowledge_source,
        knowledge_notebook=knowledge_notebook,
        knowledge_retention_max_documents=knowledge_retention_max_documents,
        knowledge_retention_max_age_days=knowledge_retention_max_age_days,
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Skills Commands
# ============================================================================

skills_app = typer.Typer(help="Manage skills")
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list(
    show_all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include unavailable skills with unmet requirements (disabled skills are always listed)",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output JSON instead of a table"),
    only_enforce_ready: bool = typer.Option(
        False,
        "--only-enforce-ready",
        help="Only include skills that are ready for --skill-perms enforce (valid manifest + permissions list)",
    ),
):
    """List skills and their status."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    skills = loader.list_skills(filter_unavailable=False)

    if not skills:
        console.print("No skills found.")
        return

    if json_out:
        out: list[dict[str, object]] = []
        for s in skills:
            name = s["name"]
            enabled = loader.is_skill_enabled(name)
            available = loader._check_requirements(loader._get_skill_meta(name))
            manifest_data, manifest_load_errors = loader.get_skill_manifest(name)
            if not show_all and not available:
                continue
            if manifest_load_errors and any(
                "manifest.json missing" in e for e in manifest_load_errors
            ):
                manifest_status = "missing"
            else:
                ok_manifest, _ = loader.validate_skill_manifest(name, strict=True)
                manifest_status = "valid" if ok_manifest else "invalid"

            perms_count = 0
            enforce_ready = False
            if not (
                manifest_load_errors
                and any("manifest.json missing" in e for e in manifest_load_errors)
            ):
                perms = (
                    manifest_data.get("permissions") if isinstance(manifest_data, dict) else None
                )
                if isinstance(perms, list) and all(isinstance(p, str) and p.strip() for p in perms):
                    perms_count = len([p for p in perms if str(p).strip()])
                    enforce_ready = True

            if only_enforce_ready and not enforce_ready:
                continue

            out.append(
                {
                    "name": name,
                    "source": s["source"],
                    "enabled": enabled,
                    "available": available,
                    "manifest": manifest_status,
                    "enforce_ready": enforce_ready,
                    "permissions_count": perms_count,
                    "scopes_count": (
                        len(
                            [
                                s
                                for s in manifest_data.get("scopes", [])
                                if isinstance(s, str) and s.strip()
                            ]
                        )
                        if isinstance(manifest_data, dict)
                        and isinstance(manifest_data.get("scopes"), list)
                        else 0
                    ),
                    "path": s["path"],
                }
            )
        console.print_json(data=out)
        return

    table = Table(title="Skills")
    table.add_column("Skill", style="cyan")
    table.add_column("Source")
    table.add_column("Enabled")
    table.add_column("Available")
    table.add_column("Manifest")
    table.add_column("EnforceReady")
    table.add_column("Perms")
    table.add_column("Scopes")
    table.add_column("Path", overflow="fold")

    for s in skills:
        name = s["name"]
        enabled = loader.is_skill_enabled(name)
        available = loader._check_requirements(loader._get_skill_meta(name))
        if not show_all and not available:
            continue
        manifest_data, manifest_load_errors = loader.get_skill_manifest(name)
        perms_count = ""
        scopes_count = ""
        enforce_ready = "no"
        if manifest_load_errors and any("manifest.json missing" in e for e in manifest_load_errors):
            perms_count = "0"
        else:
            perms = manifest_data.get("permissions") if isinstance(manifest_data, dict) else None
            if isinstance(perms, list) and all(isinstance(p, str) and p.strip() for p in perms):
                perms_count = str(len([p for p in perms if str(p).strip()]))
                enforce_ready = "yes"
            else:
                perms_count = "0"
                enforce_ready = "no"
            scopes = manifest_data.get("scopes") if isinstance(manifest_data, dict) else None
            if isinstance(scopes, list):
                scopes_count = str(len([s for s in scopes if isinstance(s, str) and s.strip()]))
            else:
                scopes_count = "0"

        if only_enforce_ready and enforce_ready != "yes":
            continue
        if manifest_load_errors and any("manifest.json missing" in e for e in manifest_load_errors):
            manifest_status = "missing"
        else:
            ok_manifest, _ = loader.validate_skill_manifest(name, strict=True)
            manifest_status = "valid" if ok_manifest else "invalid"
        table.add_row(
            name,
            s["source"],
            "[green]yes[/green]" if enabled else "[red]no[/red]",
            "[green]yes[/green]" if available else "[yellow]no[/yellow]",
            manifest_status,
            enforce_ready,
            perms_count,
            scopes_count,
            s["path"],
        )
    console.print(table)


@skills_app.command("enable")
def skills_enable(
    name: str = typer.Argument(..., help="Skill name"),
):
    """Enable a skill."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    if loader.set_skill_enabled(name, True):
        console.print(f"[green]✓[/green] Enabled skill: {name}")
    else:
        console.print(f"[red]Skill not found: {name}[/red]")


@skills_app.command("disable")
def skills_disable(
    name: str = typer.Argument(..., help="Skill name"),
):
    """Disable a skill."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    if loader.set_skill_enabled(name, False):
        console.print(f"[green]✓[/green] Disabled skill: {name}")
    else:
        console.print(f"[red]Skill not found: {name}[/red]")


@skills_app.command("test")
def skills_test(
    name: str = typer.Argument(..., help="Skill name"),
    integrity: bool = typer.Option(
        True,
        "--integrity/--no-integrity",
        help="Also verify manifest integrity hashes when declared",
    ),
    require_integrity: bool = typer.Option(
        False,
        "--require-integrity",
        help="Fail when integrity metadata is missing",
    ),
):
    """Run product-facing preflight checks for one skill and any local skill tests."""
    import pytest

    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    found = loader._find_skill(name)
    if not found:
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(1)

    manifest_ok, manifest_errors = loader.validate_skill_manifest(name, strict=True)
    integrity_ok = True
    integrity_errors: list[str] = []
    if integrity:
        integrity_ok, integrity_errors = loader.verify_skill_integrity(
            name, require_integrity=require_integrity
        )

    tests_dir = found["dir"] / "tests"  # type: ignore[index]
    test_files = sorted(tests_dir.rglob("test_*.py")) if tests_dir.exists() else []
    pytest_ok = True
    pytest_exit_code = 0
    if test_files:
        pytest_exit_code = int(pytest.main(["-q", str(tests_dir)]))
        pytest_ok = pytest_exit_code == 0

    table = Table(title=f"Skill Test: {name}")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_row("manifest", "pass" if manifest_ok else "fail")
    table.add_row("integrity", "pass" if integrity_ok else "fail")
    table.add_row("tests_dir", str(tests_dir if test_files else "not found"))
    table.add_row("pytest", "pass" if pytest_ok else f"fail (exit={pytest_exit_code})")
    console.print(table)

    for err in manifest_errors:
        console.print(f"  - manifest: {err}")
    for err in integrity_errors:
        console.print(f"  - integrity: {err}")
    if not test_files:
        console.print("  - pytest: no local tests found; preflight-only run")

    if manifest_ok and integrity_ok and pytest_ok:
        console.print(f"[green]✓[/green] Skill preflight passed: {name}")
        return
    raise typer.Exit(1)


@skills_app.command("validate")
def skills_validate(
    name: str = typer.Option("", "--name", "-n", help="Validate one skill by name"),
    strict: bool = typer.Option(False, "--strict", help="Require manifest.json for all skills"),
    integrity: bool = typer.Option(
        False,
        "--integrity",
        help="Also verify manifest integrity hashes",
    ),
    require_integrity: bool = typer.Option(
        False,
        "--require-integrity",
        help="Fail when integrity block is missing (effective only with --integrity)",
    ),
):
    """Validate skill manifest schema."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)

    if name:
        ok, errors = loader.validate_skill_manifest(name, strict=strict)
        if ok and integrity:
            ok_i, err_i = loader.verify_skill_integrity(name, require_integrity=require_integrity)
            if not ok_i:
                ok = False
                errors.extend(err_i)
        if ok:
            console.print(f"[green]✓[/green] {name}: valid")
            return
        console.print(f"[red]{name}: invalid[/red]")
        for err in errors:
            console.print(f"  - {err}")
        raise typer.Exit(1)

    results = loader.validate_all_skill_manifests(strict=strict)
    if integrity:
        integrity_rows = loader.verify_all_skill_integrity(require_integrity=require_integrity)
        by_name = {str(r.get("name") or ""): r for r in integrity_rows}
        merged: list[dict[str, object]] = []
        for row in results:
            skill_name = str(row.get("name") or "")
            ok = bool(row.get("ok"))
            errors = list(row.get("errors") or [])
            i_row = by_name.get(skill_name)
            if isinstance(i_row, dict):
                if not bool(i_row.get("ok")):
                    ok = False
                    errors.extend(list(i_row.get("errors") or []))
            merged.append({"name": skill_name, "ok": ok, "errors": errors})
        results = merged
    failed = [r for r in results if not r["ok"]]
    if not failed:
        console.print("[green]✓[/green] All skill manifests are valid")
        return

    console.print(f"[red]{len(failed)} invalid skill manifest(s)[/red]")
    for item in failed:
        console.print(f"  {item['name']}:")
        for err in item["errors"]:
            console.print(f"    - {err}")
    raise typer.Exit(1)


@skills_app.command("verify-integrity")
def skills_verify_integrity(
    name: str = typer.Option("", "--name", "-n", help="Verify one skill by name"),
    require_integrity: bool = typer.Option(
        False,
        "--require-integrity",
        help="Fail when integrity block is missing in manifest.json",
    ),
):
    """Verify skills against manifest-declared file hashes."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)

    if name:
        ok, errors = loader.verify_skill_integrity(name, require_integrity=require_integrity)
        if ok:
            console.print(f"[green]✓[/green] {name}: integrity valid")
            return
        console.print(f"[red]{name}: integrity invalid[/red]")
        for err in errors:
            console.print(f"  - {err}")
        raise typer.Exit(1)

    results = loader.verify_all_skill_integrity(require_integrity=require_integrity)
    failed = [r for r in results if not r["ok"]]
    if not failed:
        console.print("[green]✓[/green] All skills integrity checks passed")
        return

    console.print(f"[red]{len(failed)} skill integrity check(s) failed[/red]")
    for item in failed:
        console.print(f"  {item['name']}:")
        for err in item["errors"]:
            console.print(f"    - {err}")
    raise typer.Exit(1)


def _verify_ip_safe(ip_str: str) -> bool:
    """Verify if an IP address is safe (not private, loopback, or reserved)."""
    return is_public_ip(ip_str)


def _resolve_safe_ip(host: str) -> str | None:
    """Resolve a host to an IP and verify it is not a private/local address."""
    return resolve_safe_ip(host)


MAX_ARCHIVE_BYTES = 5 * 1024 * 1024  # 5MB
MAX_REDIRECTS = 5


@skills_app.command("install")
def skills_install(
    source_dir: str = typer.Argument(
        ..., help="Skill source: local dir/.zip, http(s) zip URL, or market:<name>"
    ),
    name: str = typer.Option(
        "", "--name", "-n", help="Install as this skill name (defaults to source dir name)"
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", "-f", help="Overwrite existing workspace skill"
    ),
    strict_manifest: bool = typer.Option(
        False,
        "--strict-manifest",
        help="Require manifest.json and validate it before install",
    ),
    trust_unverified: bool = typer.Option(
        False,
        "--trust-unverified",
        help="Allow downloading from non-whitelisted hosts (URL installs only)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate install only, do not write files"
    ),
):
    """Install a skill from local directory into workspace."""
    import hashlib
    import tempfile
    from urllib.parse import urlparse

    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config
    from zen_claw.skills.registry import RegistryEntry, SkillsRegistry

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    source_value = source_dir.strip()

    def _install_local(path: Path) -> tuple[bool, str]:
        if path.is_file() and path.suffix.lower() == ".zip":
            return loader.install_skill_from_zip(
                path,
                name=name or None,
                overwrite=overwrite,
                require_manifest=strict_manifest,
                dry_run=dry_run,
            )
        return loader.install_skill_from_dir(
            path,
            name=name or None,
            overwrite=overwrite,
            require_manifest=strict_manifest,
            dry_run=dry_run,
        )

    resolved_market_entry: RegistryEntry | None = None
    if source_value.lower().startswith("market:"):
        skill_name = source_value.split(":", 1)[1].strip()
        if not skill_name:
            console.print("[red]market source requires skill name, e.g. market:web-search[/red]")
            raise typer.Exit(1)
        from zen_claw.config.loader import get_data_dir

        cache_path = get_data_dir() / "skills" / config.skills_market.cache_file
        registry = SkillsRegistry(
            registry_url=config.skills_market.registry_url,
            cache_path=cache_path,
            cache_ttl_sec=config.skills_market.cache_ttl_sec,
        )
        try:
            rows = registry.fetch(force=False)
        except RuntimeError as exc:
            console.print(f"[red]Registry fetch failed:[/red] {exc}")
            raise typer.Exit(1)
        match = next((r for r in rows if r.name.lower() == skill_name.lower()), None)
        if match is None:
            console.print(f"[red]Skill not found in registry:[/red] {skill_name}")
            raise typer.Exit(1)
        if match.yanked:
            console.print(
                f"[red]Refused:[/red] registry entry is yanked: {match.name}@{match.version}"
            )
            raise typer.Exit(1)
        if not match.download_url:
            console.print(f"[red]Registry entry has no download_url:[/red] {match.name}")
            raise typer.Exit(1)
        source_value = match.download_url
        resolved_market_entry = match

    parsed = urlparse(source_value)
    is_http_source = parsed.scheme in {"http", "https"}
    if is_http_source:
        trusted_hosts = {h.strip().lower() for h in config.skills_market.trusted_hosts if h.strip()}
        registry_host = urlparse(config.skills_market.registry_url).hostname or ""
        if registry_host:
            trusted_hosts.add(registry_host.lower())

        try:
            import httpx
        except ImportError:
            console.print("[red]httpx is required for URL installs[/red]")
            raise typer.Exit(1)

        from urllib.parse import urljoin

        current_url = source_value
        payload = b""
        redirect_count = 0

        while True:
            cur_parsed = urlparse(current_url)
            source_host = (cur_parsed.hostname or "").lower()

            # 1. Trusted Host Check
            if source_host not in trusted_hosts and not trust_unverified:
                console.print(
                    f"[red]Refused host:[/red] {source_host or '(unknown)'} not in trusted hosts. "
                    "Use --trust-unverified to bypass."
                )
                raise typer.Exit(1)

            # 2. SSRF / IP Resolve Check
            safe_ip = _resolve_safe_ip(source_host)
            if not safe_ip:
                console.print(f"[red]Refused host (unsafe or unresolvable IP):[/red] {source_host}")
                raise typer.Exit(1)

            try:
                # Use follow_redirects=False for manual hop-by-hop verification
                with httpx.stream("GET", current_url, timeout=30.0, follow_redirects=False) as resp:
                    # Handle Redirects
                    if resp.status_code in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > MAX_REDIRECTS:
                            console.print("[red]Too many redirects[/red]")
                            raise typer.Exit(1)

                        location = resp.headers.get("Location")
                        if not location:
                            console.print("[red]Redirect missing Location header[/red]")
                            raise typer.Exit(1)

                        current_url = urljoin(current_url, location)
                        continue

                    resp.raise_for_status()

                    # 3. Stream content with size limit
                    content_len = resp.headers.get("Content-Length")
                    if content_len and int(content_len) > MAX_ARCHIVE_BYTES:
                        console.print(
                            f"[red]Archive too large (Content-Length: {content_len} bytes)[/red]"
                        )
                        raise typer.Exit(1)

                    downloaded = 0
                    chunks = []
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        downloaded += len(chunk)
                        if downloaded > MAX_ARCHIVE_BYTES:
                            console.print(
                                f"[red]Archive too large (exceeded {MAX_ARCHIVE_BYTES} bytes)[/red]"
                            )
                            raise typer.Exit(1)
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                    break  # Success
            except Exception as exc:
                console.print(f"[red]Download failed:[/red] {exc}")
                raise typer.Exit(1)

        if resolved_market_entry and resolved_market_entry.sha256:
            actual = hashlib.sha256(payload).hexdigest()
            expected = resolved_market_entry.sha256.strip().lower()
            if actual != expected:
                console.print(
                    f"[red]Checksum mismatch:[/red] expected {expected[:12]}..., got {actual[:12]}..."
                )
                raise typer.Exit(1)
        with tempfile.NamedTemporaryFile(
            prefix="zen-claw-skill-", suffix=".zip", delete=False
        ) as tmpf:
            tmpf.write(payload)
            tmp_path = Path(tmpf.name)
        try:
            ok, msg = _install_local(tmp_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        ok, msg = _install_local(Path(source_value))

    if ok:
        console.print(f"[green]✓[/green] {msg}")
        return
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


@skills_app.command("uninstall")
def skills_uninstall(
    name: str = typer.Argument(..., help="Skill name to uninstall from workspace"),
):
    """Uninstall a workspace skill."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    ok, msg = loader.uninstall_skill(name)
    if ok:
        console.print(f"[green]✓[/green] {msg}")
        return
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


@skills_app.command("info")
def skills_info(
    name: str = typer.Argument(..., help="Skill name"),
):
    """Show skill source, status, and manifest details."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    skills = {s["name"]: s for s in loader.list_skills(filter_unavailable=False)}
    skill = skills.get(name)
    if not skill:
        console.print(f"[red]Skill not found: {name}[/red]")
        raise typer.Exit(1)

    enabled = loader.is_skill_enabled(name)
    available = loader._check_requirements(loader._get_skill_meta(name))
    ok, errors = loader.validate_skill_manifest(name, strict=False)
    manifest, manifest_load_errors = loader.get_skill_manifest(name)
    hardening = bool(config.tools.policy.production_hardening)
    default_mode = str(config.agents.defaults.skill_permissions_mode)
    effective_mode_if_loaded = "enforce" if hardening else default_mode

    # Preflight for the runtime permission gate (aligned with AgentLoop enforcement).
    preflight_errors: list[str] = []
    if manifest_load_errors:
        preflight_errors.extend(manifest_load_errors)
    perms = (manifest or {}).get("permissions") if isinstance(manifest, dict) else None
    if not isinstance(perms, list) or not all(
        isinstance(p, str) and p.strip() for p in perms or []
    ):
        preflight_errors.append("permissions missing or invalid in manifest.json")
    enforce_ready = len(preflight_errors) == 0

    table = Table(title=f"Skill: {name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("source", skill["source"])
    table.add_row("path", skill["path"])
    table.add_row("enabled", "yes" if enabled else "no")
    table.add_row("available", "yes" if available else "no")
    table.add_row("manifest_valid", "yes" if ok else "no")
    table.add_row("skillPermsDefault", default_mode)
    table.add_row("skillPermsEffectiveIfLoaded", effective_mode_if_loaded)
    table.add_row("hardeningForcesEnforceOnSkillSlot", "yes" if hardening else "no")
    table.add_row("skillPermsEnforceReady", "yes" if enforce_ready else "no")
    if preflight_errors:
        table.add_row("skillPermsEnforcePreflightErrors", "; ".join(preflight_errors))
    if errors:
        table.add_row("manifest_errors", "; ".join(errors))
    if manifest_load_errors:
        table.add_row("manifest_load", "; ".join(manifest_load_errors))
    if manifest:
        table.add_row("manifest_version", str(manifest.get("version", "")))
        perms = manifest.get("permissions", [])
        table.add_row(
            "manifest_permissions",
            ", ".join(perms) if isinstance(perms, list) else str(perms),
        )
        scopes = manifest.get("scopes", [])
        table.add_row(
            "manifest_scopes",
            ", ".join(scopes) if isinstance(scopes, list) else str(scopes),
        )
    console.print(table)


@skills_app.command("export")
def skills_export(
    name: str = typer.Argument(..., help="Skill name to export"),
    out: str = typer.Option("", "--out", "-o", help="Output zip file path"),
    overwrite: bool = typer.Option(
        False, "--overwrite", "-f", help="Overwrite output zip if exists"
    ),
):
    """Export a skill to a .zip archive."""
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    if out.strip():
        out_path = Path(out)
    else:
        out_path = config.workspace_path / ".zen-claw" / "exports" / f"{name}.zip"
    ok, msg, *_ = loader.export_skill_to_zip(name, out_path, overwrite=overwrite)
    if ok:
        console.print(f"[green]✓[/green] {msg}")
        return
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


@skills_app.command("sbom")
def skills_sbom(
    out: str = typer.Option("", "--out", "-o", help="Write SBOM json to file path"),
):
    """Export skills inventory as a SBOM-style JSON document."""
    import json

    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import load_config

    config = load_config()
    loader = SkillsLoader(config.workspace_path)
    doc = loader.build_skills_sbom()
    if out.strip():
        out_path = Path(out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[green]✓[/green] SBOM exported: {out_path}")
        return
    console.print_json(data=doc)


@skills_app.command("search")
def skills_search(
    query: str = typer.Argument("", help="Search keyword"),
    tag: str = typer.Option("", "--tag", help="Filter by tag"),
    author: str = typer.Option("", "--author", help="Filter by author"),
    enforce_ready: bool | None = typer.Option(
        None, "--enforce-ready/--no-enforce-ready", help="Filter enforce_ready"
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Force refresh registry cache"),
):
    """Search skills market registry."""
    from zen_claw.config.loader import get_data_dir, load_config
    from zen_claw.skills.registry import SkillsRegistry

    cfg = load_config()
    cache_path = get_data_dir() / "skills" / cfg.skills_market.cache_file
    registry = SkillsRegistry(
        registry_url=cfg.skills_market.registry_url,
        cache_path=cache_path,
        cache_ttl_sec=cfg.skills_market.cache_ttl_sec,
    )
    try:
        rows = registry.search(
            query=query,
            tag=tag,
            author=author,
            enforce_ready=enforce_ready,
            force_refresh=refresh,
        )
    except RuntimeError as exc:
        console.print(f"[red]Registry fetch failed:[/red] {exc}")
        raise typer.Exit(1)
    if not rows:
        console.print("[yellow]No skills found.[/yellow]")
        return
    table = Table(title="Skills Market")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Author")
    table.add_column("Tags")
    table.add_column("Integrity")
    for row in rows:
        table.add_row(
            row.name,
            row.version,
            row.author or "-",
            ", ".join(row.tags) if row.tags else "-",
            "ready" if row.enforce_ready else "basic",
        )
    console.print(table)


@skills_app.command("publish")
def skills_publish(
    name: str = typer.Argument(..., help="Skill name"),
    skip_integrity: bool = typer.Option(
        False, "--skip-integrity", help="Skip integrity requirement"
    ),
    out_dir: str = typer.Option("", "--out-dir", help="Output directory"),
):
    """Package skill and generate catalog entry JSON."""
    from zen_claw.config.loader import load_config
    from zen_claw.skills.publisher import SkillsPublisher

    cfg = load_config()
    workspace = cfg.workspace_path
    output_dir = (
        Path(out_dir) if out_dir.strip() else (workspace / cfg.skills_market.publish_output_dir)
    )
    require_integrity = cfg.skills_market.publish_require_integrity and not skip_integrity
    publisher = SkillsPublisher(
        workspace=workspace, output_dir=output_dir, require_integrity=require_integrity
    )
    result = publisher.publish(name)
    if not result.ok:
        console.print(f"[red]Publish failed:[/red] {result.error}")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Package: {result.zip_path}")
    console.print(f"[green]✓[/green] Catalog entry: {result.catalog_entry_path}")
    console.print(f"[green]✓[/green] SHA-256: {result.sha256}")


# ============================================================================
# Multi-tenant Commands
# ============================================================================

tenant_app = typer.Typer(help="Manage tenants")
app.add_typer(tenant_app, name="tenant")

user_app = typer.Typer(help="Manage users")
app.add_typer(user_app, name="user")


@tenant_app.command("create")
def tenant_create(
    name: str = typer.Argument(..., help="Tenant display name"),
    quota_llm: int = typer.Option(1000, "--quota-llm", help="LLM calls per day"),
    quota_storage: int = typer.Option(1000, "--quota-storage", help="Storage MB"),
    store_backend: str = typer.Option("", "--store-backend", help="Tenant RAG backend policy"),
):
    from zen_claw.auth.tenant import TenantStore
    from zen_claw.config.loader import get_data_dir

    store = TenantStore(get_data_dir())
    tenant = store.create(
        name,
        quota_llm_calls_per_day=quota_llm,
        quota_storage_mb=quota_storage,
        store_backend=store_backend,
    )
    console.print(
        f"[green]Tenant created[/green]: {tenant.tenant_id} ({tenant.name}) "
        f"backend={tenant.store_backend or 'default'}"
    )


@tenant_app.command("list")
def tenant_list():
    import datetime

    from zen_claw.auth.tenant import TenantStore
    from zen_claw.config.loader import get_data_dir

    store = TenantStore(get_data_dir())
    rows = store.list()
    table = Table(title="Tenants")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("LLM/day")
    table.add_column("Backend")
    table.add_column("Created")
    for row in rows:
        created = datetime.datetime.fromtimestamp(row.created_at).strftime("%Y-%m-%d")
        table.add_row(
            row.tenant_id[:8] + "...",
            row.name,
            "Yes" if row.enabled else "No",
            str(row.quota_llm_calls_per_day),
            row.store_backend or "default",
            created,
        )
    console.print(table)


@tenant_app.command("set-backend")
def tenant_set_backend(
    tenant_id: str = typer.Argument(..., help="Tenant ID"),
    store_backend: str = typer.Option(
        "", "--store-backend", help="Tenant RAG backend policy (chroma|memory)"
    ),
):
    from zen_claw.auth.tenant import TenantStore
    from zen_claw.config.loader import get_data_dir

    store = TenantStore(get_data_dir())
    try:
        tenant = store.set_store_backend(tenant_id, store_backend)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]Updated[/green] tenant {tenant.tenant_id} backend={tenant.store_backend or 'default'}"
    )


@user_app.command("create")
def user_create(
    username: str = typer.Argument(..., help="Username"),
    tenant: str = typer.Option(..., "--tenant", "-t", help="Tenant ID"),
    role: str = typer.Option("member", "--role", "-r", help="Role: admin|member"),
    password: str = typer.Option("", "--password", "-p", help="Password"),
):
    from zen_claw.auth.user import UserStore
    from zen_claw.config.loader import get_data_dir

    if not password:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    store = UserStore(get_data_dir())
    try:
        user = store.create(tenant_id=tenant, username=username, password=password, role=role)  # type: ignore[arg-type]
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[green]User created[/green]: {user.user_id} ({user.username})")


@user_app.command("list")
def user_list(tenant: str = typer.Option(..., "--tenant", "-t", help="Tenant ID")):
    import datetime

    from zen_claw.auth.user import UserStore
    from zen_claw.config.loader import get_data_dir

    store = UserStore(get_data_dir())
    rows = store.list_by_tenant(tenant)
    table = Table(title=f"Users ({tenant[:8]}...)")
    table.add_column("ID")
    table.add_column("Username")
    table.add_column("Role")
    table.add_column("Enabled")
    table.add_column("Created")
    for row in rows:
        created = datetime.datetime.fromtimestamp(row.created_at).strftime("%Y-%m-%d")
        table.add_row(
            row.user_id[:8] + "...", row.username, row.role, "Yes" if row.enabled else "No", created
        )
    console.print(table)


# ============================================================================
# TTS Commands
# ============================================================================

tts_app = typer.Typer(help="Text-to-Speech commands")
app.add_typer(tts_app, name="tts")


@tts_app.command("synthesize")
def tts_synthesize(
    text: str = typer.Argument(..., help="Text to synthesize"),
    voice: str = typer.Option("", "--voice", "-v", help="Voice name"),
    output: str = typer.Option("", "--output", "-o", help="Output file path"),
    fmt: str = typer.Option("mp3", "--format", "-f", help="Output format: mp3 or wav"),
    provider: str = typer.Option(
        "", "--provider", "-p", help="Override provider: edge|minimax|openai"
    ),
):
    """Synthesize text to speech."""
    from zen_claw.config.loader import load_config
    from zen_claw.providers.tts import (
        EdgeTTSProvider,
        MinimaxTTSProvider,
        OpenAITTSProvider,
        get_tts_provider,
    )

    cfg = load_config()
    if provider:
        if provider == "edge":
            tts = EdgeTTSProvider(default_voice=voice or None)
        elif provider == "minimax":
            tts = MinimaxTTSProvider()
        elif provider == "openai":
            tts = OpenAITTSProvider()
        else:
            console.print(f"[red]Unknown provider:[/red] {provider}")
            raise typer.Exit(1)
    else:
        try:
            tts = get_tts_provider(cfg)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    if not output:
        output = f"tts_output_{int(time.time())}.{fmt}"
    out_path = Path(output)

    async def _run() -> None:
        await tts.synthesize_to_file(
            text=text, output_path=out_path, voice=voice or None, output_format=fmt
        )
        console.print(f"[green]Done:[/green] {out_path} ({out_path.stat().st_size} bytes)")

    try:
        asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]TTS error:[/red] {exc}")
        raise typer.Exit(1)


@tts_app.command("list-voices")
def tts_list_voices(locale: str = typer.Option("zh", "--locale", "-l", help="Locale filter")):
    """List Edge TTS voices."""
    from zen_claw.providers.tts import EdgeTTSProvider

    async def _run() -> None:
        rows = await EdgeTTSProvider.list_voices(locale_filter=locale)
        table = Table(title=f"EdgeTTS Voices ({locale})")
        table.add_column("Name")
        table.add_column("Locale")
        table.add_column("Gender")
        for row in rows:
            table.add_row(row["name"], row["locale"], row["gender"])
        console.print(table)

    asyncio.run(_run())


# ============================================================================
# API Key Commands (REST gateway)
# ============================================================================

api_key_app = typer.Typer(help="Manage API keys for REST gateway")
app.add_typer(api_key_app, name="api-key")


@api_key_app.command("generate")
def api_key_generate():
    """Generate and store a new API key."""
    from zen_claw.dashboard.server import generate_api_key, store_api_key

    raw, prefix = generate_api_key()
    store_api_key(raw)
    console.print("[green]Generated API key (shown once):[/green]")
    console.print(f"[bold yellow]{raw}[/bold yellow]")
    console.print(f"Prefix: {prefix}")


@api_key_app.command("list")
def api_key_list():
    """List stored API key prefixes."""
    import datetime

    from zen_claw.dashboard.server import _load_api_keys

    rows = _load_api_keys()
    table = Table(title="API Keys")
    table.add_column("Prefix")
    table.add_column("Created")
    table.add_column("Enabled")
    for _, row in rows.items():
        created = datetime.datetime.fromtimestamp(int(row.get("created_at", 0))).strftime(
            "%Y-%m-%d %H:%M"
        )
        table.add_row(
            str(row.get("prefix", "")) + "...", created, "Yes" if row.get("enabled", True) else "No"
        )
    console.print(table)


@api_key_app.command("revoke")
def api_key_revoke(prefix: str = typer.Argument(..., help="Key prefix to revoke")):
    """Revoke one or more keys by prefix."""
    from zen_claw.dashboard.server import revoke_api_key_by_prefix

    ok = revoke_api_key_by_prefix(prefix)
    if not ok:
        console.print(f"[red]No keys matched prefix:[/red] {prefix}")
        raise typer.Exit(1)
    console.print(f"[green]Revoked keys with prefix:[/green] {prefix}")


# ============================================================================
# Knowledge Commands (RAG)
# ============================================================================

knowledge_app = typer.Typer(help="Knowledge base (RAG) commands")
app.add_typer(knowledge_app, name="knowledge")
rag_app = typer.Typer(help="RAG pipeline commands")
app.add_typer(rag_app, name="rag")
crawler_app = typer.Typer(help="Crawler accelerator commands")
app.add_typer(crawler_app, name="crawler")


@crawler_app.command("run")
def crawler_run(
    url: str = typer.Argument(..., help="Source URL to crawl"),
    name: str = typer.Option("crawler", "--name", help="Crawler source name"),
    notebook: str = typer.Option("default", "--notebook", "-n", help="Target notebook"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    selector: str = typer.Option("", "--selector", help="Optional CSS selector (browser mode only)"),
    browser: bool = typer.Option(False, "--browser", help="Use browser sidecar extraction"),
    max_chars: int = typer.Option(20000, "--max-chars", help="Maximum extracted characters"),
    metadata_json: str = typer.Option("", "--metadata-json", help="Optional JSON object metadata"),
):
    """Run one crawler extraction and ingest the result into RAG."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.skills.crawler.extractor import CrawlerExtractor, CrawlerSource

    async def _run() -> None:
        metadata = json.loads(metadata_json) if metadata_json.strip() else {}
        if metadata and not isinstance(metadata, dict):
            console.print("[red]Error:[/red] --metadata-json must be a JSON object")
            raise typer.Exit(1)
        extractor = CrawlerExtractor(get_data_dir(), tenant_id=tenant, store_kind=store_backend)
        payload = await extractor.crawl_to_rag(
            CrawlerSource(
                name=name,
                url=url,
                notebook_id=notebook,
                selector=selector,
                use_browser=browser,
                max_chars=max_chars,
                metadata=dict(metadata),
            )
        )
        console.print(
            f"[green]Crawled[/green] [cyan]{payload.get('crawl_name', name)}[/cyan] "
            f"-> notebook [cyan]{payload.get('notebook', notebook)}[/cyan]"
        )
        console.print(
            f"documents={payload.get('documents', 0)} "
            f"chunks_added={payload.get('chunks_added', 0)} "
            f"mode={payload.get('crawl_mode', '-')}"
        )

    asyncio.run(_run())


@crawler_app.command("run-source")
def crawler_run_source(
    source_name: str = typer.Argument(..., help="Crawler source name from the shared catalog"),
    source_file: str = typer.Option("", "--source-file", help="Optional crawler sources JSON file"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
):
    """Run one crawler source from the shared source catalog."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.skills.crawler.extractor import CrawlerExtractor
    from zen_claw.skills.crawler.scheduler import get_source_by_name, resolve_sources_path

    async def _run() -> None:
        catalog_path = resolve_sources_path(get_data_dir(), source_file or None)
        source = get_source_by_name(catalog_path, source_name)
        extractor = CrawlerExtractor(get_data_dir(), tenant_id=tenant, store_kind=store_backend)
        payload = await extractor.crawl_to_rag(source)
        console.print(
            f"[green]Crawled[/green] [cyan]{payload.get('crawl_name', source.name)}[/cyan] "
            f"-> notebook [cyan]{payload.get('notebook', source.notebook_id)}[/cyan]"
        )
        console.print(
            f"documents={payload.get('documents', 0)} "
            f"chunks_added={payload.get('chunks_added', 0)} "
            f"mode={payload.get('crawl_mode', '-')} "
            f"change={payload.get('change_status', '-')}"
        )

    asyncio.run(_run())


@crawler_app.command("schedule")
def crawler_schedule(
    url: str = typer.Argument(..., help="Source URL to crawl"),
    name: str = typer.Option(..., "--name", "-n", help="Cron job and crawler source name"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    notebook: str = typer.Option("default", "--notebook", help="Target notebook"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    selector: str = typer.Option("", "--selector", help="Optional CSS selector (browser mode only)"),
    browser: bool = typer.Option(False, "--browser", help="Use browser sidecar extraction"),
    max_chars: int = typer.Option(20000, "--max-chars", help="Maximum extracted characters"),
    metadata_json: str = typer.Option("", "--metadata-json", help="Optional JSON object metadata"),
    message: str = typer.Option("", "--message", "-m", help="Cron audit message"),
):
    """Schedule a crawler job on top of the existing cron service."""
    import datetime

    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService
    from zen_claw.cron.types import CronSchedule
    from zen_claw.skills.crawler.extractor import CrawlerSource
    from zen_claw.skills.crawler.scheduler import build_crawler_cron_kwargs

    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    metadata = json.loads(metadata_json) if metadata_json.strip() else {}
    if metadata and not isinstance(metadata, dict):
        console.print("[red]Error:[/red] --metadata-json must be a JSON object")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(store_path)
    source = CrawlerSource(
        name=name,
        url=url,
        notebook_id=notebook,
        selector=selector,
        use_browser=browser,
        max_chars=max_chars,
        metadata=dict(metadata),
    )
    job = cron.add_job(
        name=name,
        schedule=schedule,
        message=message or f"crawl {url}",
        **build_crawler_cron_kwargs(
            source,
            tenant_id=tenant,
            store_backend=store_backend,
        ),
    )
    console.print(f"[green]Added crawler cron job[/green] {job.id} -> {name}")


@crawler_app.command("schedule-source")
def crawler_schedule_source(
    source_name: str = typer.Argument(..., help="Crawler source name from the shared catalog"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    source_file: str = typer.Option("", "--source-file", help="Optional crawler sources JSON file"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    message: str = typer.Option("", "--message", "-m", help="Cron audit message"),
):
    """Schedule one crawler source from the shared catalog."""
    import datetime

    from zen_claw.config.loader import get_data_dir
    from zen_claw.cron.service import CronService
    from zen_claw.cron.types import CronSchedule
    from zen_claw.skills.crawler.scheduler import (
        build_crawler_cron_kwargs,
        get_source_by_name,
        resolve_sources_path,
    )

    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    catalog_path = resolve_sources_path(get_data_dir(), source_file or None)
    source = get_source_by_name(catalog_path, source_name)
    store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(store_path)
    job = cron.add_job(
        name=source.name,
        schedule=schedule,
        message=message or f"crawl {source.url}",
        **build_crawler_cron_kwargs(
            source,
            tenant_id=tenant,
            store_backend=store_backend,
        ),
    )
    console.print(f"[green]Added crawler cron job[/green] {job.id} -> {source.name}")


@crawler_app.command("source-list")
def crawler_source_list(
    source_file: str = typer.Option("", "--source-file", help="Optional crawler sources JSON file"),
):
    """List crawler sources from the shared source catalog."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.skills.crawler.scheduler import load_sources_json, resolve_sources_path

    catalog_path = resolve_sources_path(get_data_dir(), source_file or None)
    if not catalog_path.exists():
        console.print(f"[yellow]No crawler source catalog found:[/yellow] {catalog_path}")
        raise typer.Exit(0)
    rows = load_sources_json(catalog_path)
    table = Table(title=f"Crawler Sources: {catalog_path}")
    table.add_column("Name")
    table.add_column("URL")
    table.add_column("Notebook")
    table.add_column("Mode")
    table.add_column("Selector")
    for row in rows:
        table.add_row(
            row.name,
            row.url,
            row.notebook_id,
            "browser" if row.use_browser else "http",
            row.selector or "-",
        )
    console.print(table)


@crawler_app.command("source-add")
def crawler_source_add(
    url: str = typer.Argument(..., help="Source URL to add to the shared crawler catalog"),
    name: str = typer.Option(..., "--name", "-n", help="Crawler source name"),
    notebook: str = typer.Option("default", "--notebook", help="Target notebook"),
    selector: str = typer.Option("", "--selector", help="Optional CSS selector"),
    browser: bool = typer.Option(False, "--browser", help="Use browser extraction"),
    max_chars: int = typer.Option(20000, "--max-chars", help="Maximum extracted characters"),
    metadata_json: str = typer.Option("", "--metadata-json", help="Optional JSON object metadata"),
    source_file: str = typer.Option("", "--source-file", help="Optional crawler sources JSON file"),
):
    """Add or update one crawler source in the shared source catalog."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.skills.crawler.extractor import CrawlerSource
    from zen_claw.skills.crawler.scheduler import resolve_sources_path, upsert_source

    metadata = json.loads(metadata_json) if metadata_json.strip() else {}
    if metadata and not isinstance(metadata, dict):
        console.print("[red]Error:[/red] --metadata-json must be a JSON object")
        raise typer.Exit(1)

    catalog_path = resolve_sources_path(get_data_dir(), source_file or None)
    upsert_source(
        catalog_path,
        CrawlerSource(
            name=name,
            url=url,
            notebook_id=notebook,
            selector=selector,
            use_browser=browser,
            max_chars=max_chars,
            metadata=dict(metadata),
        ),
    )
    console.print(f"[green]Saved crawler source[/green] {name} -> {catalog_path}")


@knowledge_app.command("add")
def knowledge_add(
    source: str = typer.Argument(..., help="File path or URL to ingest"),
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    metadata_json: str = typer.Option(
        "", "--metadata-json", help="Optional JSON object to attach as chunk metadata"
    ),
):
    """Add content to the knowledge base."""
    from zen_claw.agent.tools.knowledge import KnowledgeAddTool
    from zen_claw.config.loader import get_data_dir

    async def _run() -> None:
        tool = KnowledgeAddTool(data_dir=get_data_dir(), tenant_id=tenant, store_kind=store_backend)
        metadata = json.loads(metadata_json) if metadata_json.strip() else None
        if metadata is not None and not isinstance(metadata, dict):
            console.print("[red]Error:[/red] --metadata-json must be a JSON object")
            raise typer.Exit(1)
        result = await tool.execute(
            source=source,
            notebook_id=notebook,
            metadata=metadata,
            tenant_id=tenant,
            store_backend=store_backend,
        )
        if not result.ok:
            msg = result.error.message if result.error else "unknown"
            console.print(f"[red]Error:[/red] {msg}")
            raise typer.Exit(1)
        data = json.loads(result.content)
        console.print(
            f"[green]Added[/green] {data.get('chunks_added', 0)} chunks to notebook "
            f"[cyan]{data.get('notebook', notebook)}[/cyan]"
        )

    import json

    asyncio.run(_run())


@knowledge_app.command("search")
def knowledge_search(
    query: str = typer.Argument(..., help="Search query"),
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Result count"),
    filters_json: str = typer.Option(
        "", "--filters-json", help="Optional JSON object for exact-match metadata filters"
    ),
):
    """Search knowledge base."""
    from zen_claw.agent.tools.knowledge import KnowledgeSearchTool
    from zen_claw.config.loader import get_data_dir

    async def _run() -> None:
        tool = KnowledgeSearchTool(
            data_dir=get_data_dir(),
            default_notebook=notebook,
            tenant_id=tenant,
            store_kind=store_backend,
        )
        filters = json.loads(filters_json) if filters_json.strip() else None
        if filters is not None and not isinstance(filters, dict):
            console.print("[red]Error:[/red] --filters-json must be a JSON object")
            raise typer.Exit(1)
        result = await tool.execute(
            query=query,
            notebook_id=notebook,
            top_k=top_k,
            filters=filters,
            tenant_id=tenant,
            store_backend=store_backend,
        )
        if not result.ok:
            msg = result.error.message if result.error else "unknown"
            console.print(f"[red]Error:[/red] {msg}")
            raise typer.Exit(1)
        data = json.loads(result.content)
        rows = data.get("results", [])
        if not rows:
            console.print("[yellow]No results found.[/yellow]")
            return
        for i, row in enumerate(rows, 1):
            source = row.get("source", "")
            score = row.get("score", 0.0)
            console.print(f"[bold]{i}.[/bold] {source} [dim](score={score:.3f})[/dim]")
            console.print(str(row.get("content", ""))[:300])
        if data.get("filters"):
            console.print(f"[dim]filters={json.dumps(data['filters'], ensure_ascii=False)}[/dim]")

    import json

    asyncio.run(_run())


@knowledge_app.command("remove")
def knowledge_remove(
    document_id: str = typer.Argument(..., help="Stable document ID to remove"),
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
):
    """Remove a document from the knowledge base by stable document ID."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.knowledge.pipeline import RAGPipeline

    try:
        payload = RAGPipeline(
            get_data_dir(), tenant_id=tenant, store_kind=store_backend
        ).delete_document(
            document_id, notebook_id=notebook
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]Removed[/green] {payload['documents_removed']} document(s) / "
        f"{payload['chunks_deleted']} chunk(s) from [cyan]{payload['notebook']}[/cyan]"
    )


@knowledge_app.command("documents")
def knowledge_documents(
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
):
    """List notebook documents with stable document IDs."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.knowledge.pipeline import RAGPipeline

    try:
        payload = RAGPipeline(
            get_data_dir(), tenant_id=tenant, store_kind=store_backend
        ).list_documents(notebook_id=notebook)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    rows = payload.get("documents", [])
    if not rows:
        console.print("[yellow]No documents found.[/yellow]")
        return
    table = Table(title=f"Knowledge Documents ({payload['notebook']})")
    table.add_column("Document ID")
    table.add_column("Source")
    table.add_column("Doc Units")
    table.add_column("Created")
    for row in rows:
        table.add_row(
            str(row.get("document_id", "")),
            str(row.get("source", "")),
            str(row.get("doc_units", 0)),
            str(row.get("created_at", "")),
        )
    console.print(table)


@knowledge_app.command("clear")
def knowledge_clear(
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
):
    """Remove all documents from a notebook while keeping the notebook itself."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.knowledge.pipeline import RAGPipeline

    try:
        payload = RAGPipeline(
            get_data_dir(), tenant_id=tenant, store_kind=store_backend
        ).clear_notebook(notebook_id=notebook)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]Cleared[/green] {payload['documents_removed']} document(s) / "
        f"{payload['chunks_deleted']} chunk(s) from [cyan]{payload['notebook']}[/cyan]"
    )


@knowledge_app.command("retain")
def knowledge_retain(
    notebook: str = typer.Option("default", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
    max_documents: int = typer.Option(0, "--max-documents", help="Keep at most N newest documents"),
    max_age_days: int = typer.Option(0, "--max-age-days", help="Delete documents older than N days"),
):
    """Run retention cleanup for a notebook."""
    from zen_claw.config.loader import get_data_dir, load_config
    from zen_claw.knowledge.pipeline import RAGPipeline

    cfg = load_config()
    effective_max_documents = int(max_documents or cfg.knowledge.retention_max_documents or 0)
    effective_max_age_days = int(max_age_days or cfg.knowledge.retention_max_age_days or 0)
    try:
        payload = RAGPipeline(
            get_data_dir(), tenant_id=tenant, store_kind=store_backend
        ).run_retention(
            notebook_id=notebook,
            max_documents=effective_max_documents,
            max_age_days=effective_max_age_days,
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]Retention complete[/green] {payload['documents_removed']} document(s) / "
        f"{payload['chunks_deleted']} chunk(s) removed from [cyan]{payload['notebook']}[/cyan]"
    )


@knowledge_app.command("list")
def knowledge_list(
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
):
    """List knowledge notebooks."""
    from zen_claw.agent.tools.knowledge import KnowledgeListTool
    from zen_claw.config.loader import get_data_dir

    async def _run() -> None:
        tool = KnowledgeListTool(data_dir=get_data_dir(), tenant_id=tenant)
        result = await tool.execute()
        if not result.ok:
            msg = result.error.message if result.error else "unknown"
            console.print(f"[red]Error:[/red] {msg}")
            raise typer.Exit(1)
        data = json.loads(result.content)
        rows = data.get("notebooks", [])
        if not rows:
            console.print("[yellow]No notebooks found.[/yellow]")
            return
        table = Table(title="Knowledge Notebooks")
        table.add_column("Name")
        table.add_column("ID")
        table.add_column("Docs")
        table.add_column("Backend")
        for row in rows:
            table.add_row(
                str(row.get("name", "")),
                str(row.get("id", "")),
                str(row.get("doc_count", 0)),
                str(row.get("store_backend", "") or "default"),
            )
        console.print(table)

    import json

    asyncio.run(_run())


@knowledge_app.command("stats")
def knowledge_stats(
    notebook: str = typer.Option("", "--notebook", "-n", help="Notebook name"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
    store_backend: str = typer.Option("", "--store-backend", help="Optional store backend override"),
):
    """Show knowledge/RAG stats."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.knowledge.pipeline import RAGPipeline

    payload = RAGPipeline(
        get_data_dir(), tenant_id=tenant, store_kind=store_backend
    ).stats(notebook_id=notebook)
    table = Table(title="Knowledge Stats")
    table.add_column("Notebook")
    table.add_column("Docs")
    table.add_column("Chunks")
    table.add_column("Backend")
    for row in payload["notebooks"]:
        table.add_row(
            str(row["name"]),
            str(row["doc_count"]),
            str(row["chunk_count"]),
            str(row.get("store_backend", "") or "default"),
        )
    console.print(table)
    console.print(
        f"total notebooks={payload['total_notebooks']} "
        f"documents={payload['total_documents']} "
        f"chunks={payload['total_chunks']} "
        f"rag_available={payload['rag_available']}"
    )


@knowledge_app.command("notebooks")
def knowledge_notebooks(
    create: str = typer.Option("", "--create", help="Create notebook name"),
    set_backend: str = typer.Option("", "--set-backend", help="Update notebook backend policy"),
    store_backend: str = typer.Option(
        "", "--store-backend", help="Notebook backend policy (chroma|memory)"
    ),
    tenant: str = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
):
    """Create notebook or update its backend policy."""
    action_count = int(bool(create.strip())) + int(bool(set_backend.strip()))
    if action_count != 1:
        console.print("Use either --create <name> or --set-backend <name>.")
        return
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.config.loader import get_data_dir
    from zen_claw.knowledge.notebook import NotebookManager

    manager = NotebookManager(tenant_data_dir(get_data_dir(), tenant))
    try:
        if create.strip():
            nb = manager.create(create, store_backend=store_backend)
            action = "Created"
        else:
            nb = manager.set_store_backend(set_backend, store_backend)
            action = "Updated"
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    effective_backend = nb.store_backend or "default"
    console.print(
        f"[green]{action}[/green] notebook {nb.name} ({nb.id}) backend={effective_backend}"
    )


rag_app.command("ingest")(knowledge_add)
rag_app.command("search")(knowledge_search)
rag_app.command("delete")(knowledge_remove)
rag_app.command("documents")(knowledge_documents)
rag_app.command("clear")(knowledge_clear)
rag_app.command("retain")(knowledge_retain)
rag_app.command("stats")(knowledge_stats)


# ============================================================================
# Node Commands (mobile node PoC)
# ============================================================================

node_app = typer.Typer(help="Manage mobile node registry and tasks")
app.add_typer(node_app, name="node")


def _node_service():
    from zen_claw.config.loader import get_data_dir
    from zen_claw.node.service import NodeService

    return NodeService(get_data_dir() / "nodes" / "state.json")


@node_app.command("register")
def node_register(
    name: str = typer.Option(..., "--name", help="Node display name"),
    platform: str = typer.Option("android", "--platform", help="Node platform (android/ios/other)"),
    capability: list[str] = typer.Option([], "--capability", help="Node capability (repeatable)"),
):
    """Register a node and return node_id/token."""
    svc = _node_service()
    out = svc.register_node(name=name, platform=platform, capabilities=capability)
    console.print_json(data=out)


@node_app.command("heartbeat")
def node_heartbeat(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    token: str = typer.Option(..., "--token", help="Node token"),
):
    """Send node heartbeat."""
    svc = _node_service()
    ok = svc.heartbeat(node_id=node_id, token=token)
    if not ok:
        console.print("[red]heartbeat rejected (invalid node/token)[/red]")
        raise typer.Exit(1)
    console.print("[green]✓[/green] heartbeat accepted")


@node_app.command("list")
def node_list():
    """List registered nodes."""
    svc = _node_service()
    nodes = svc.list_nodes()
    if not nodes:
        console.print("No nodes registered.")
        return
    console.print_json(data=nodes)


node_policy_app = typer.Typer(help="Manage node policy")
node_app.add_typer(node_policy_app, name="policy")


@node_policy_app.command("show")
def node_policy_show(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
):
    """Show node policy."""
    svc = _node_service()
    policy = svc.get_policy(node_id=node_id)
    if not policy:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    console.print_json(data=policy)


@node_policy_app.command("set")
def node_policy_set(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    allow_task_type: list[str] = typer.Option(
        [],
        "--allow-task-type",
        help="Allowed task type pattern (repeatable, supports '*' and 'prefix.*')",
    ),
    max_running_tasks: int = typer.Option(
        0,
        "--max-running-tasks",
        help="Max concurrent leased/running tasks (0 means keep current)",
    ),
    allow_gateway_tasks: bool | None = typer.Option(
        None,
        "--allow-gateway-tasks/--deny-gateway-tasks",
        help="Allow or deny gateway-side task execution for this node",
    ),
    require_approval_task_type: list[str] = typer.Option(
        [],
        "--require-approval-task-type",
        help="Task type pattern requiring approval before execution (repeatable)",
    ),
    approval_timeout_sec: int = typer.Option(
        -1,
        "--approval-timeout-sec",
        help="Approval timeout in seconds (0 disables timeout, -1 keeps current)",
    ),
    approval_required_count: int = typer.Option(
        0,
        "--approval-required-count",
        help="Number of distinct approvers required (0 keeps current)",
    ),
):
    """Update node policy."""
    svc = _node_service()
    policy = svc.update_policy(
        node_id=node_id,
        allowed_task_types=allow_task_type if allow_task_type else None,
        allow_gateway_tasks=allow_gateway_tasks,
        max_running_tasks=max_running_tasks if max_running_tasks > 0 else None,
        require_approval_task_types=require_approval_task_type
        if require_approval_task_type
        else None,
        approval_timeout_sec=approval_timeout_sec if approval_timeout_sec >= 0 else None,
        approval_required_count=approval_required_count if approval_required_count > 0 else None,
    )
    if not policy:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    console.print_json(data=policy)


node_token_app = typer.Typer(help="Manage node token lifecycle")
node_app.add_typer(node_token_app, name="token")


@node_token_app.command("show")
def node_token_show(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    show_token: bool = typer.Option(False, "--show-token", help="Show full token (sensitive)"),
):
    """Show node token status."""
    svc = _node_service()
    row = svc.get_token_status(node_id=node_id)
    if not row:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    token = str(row.get("token") or "")
    row["token"] = token if show_token else (token[:6] + "***" if token else "")
    console.print_json(data=row)


@node_token_app.command("rotate")
def node_token_rotate(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    ttl_sec: int = typer.Option(0, "--ttl-sec", help="Token TTL seconds (0 means keep default)"),
):
    """Rotate node token and optionally reset TTL."""
    svc = _node_service()
    row = svc.rotate_token(node_id=node_id, ttl_sec=ttl_sec if ttl_sec > 0 else None)
    if not row:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    console.print_json(data=row)


@node_token_app.command("revoke")
def node_token_revoke(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
):
    """Revoke node token."""
    svc = _node_service()
    ok = svc.revoke_token(node_id=node_id)
    if not ok:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    console.print("[green]✓[/green] token revoked")


@node_token_app.command("scan")
def node_token_scan(
    within_sec: int = typer.Option(
        3600,
        "--within-sec",
        help="Expiring window seconds (0 means only expired/revoked)",
    ),
    rotate: bool = typer.Option(
        False,
        "--rotate",
        help="Rotate candidate tokens immediately",
    ),
    ttl_sec: int = typer.Option(
        0,
        "--ttl-sec",
        help="TTL seconds to apply when rotating (0 means default)",
    ),
):
    """Scan token rotation candidates and optionally rotate them."""
    svc = _node_service()
    result = svc.scan_token_rotation(
        within_sec=within_sec,
        rotate=rotate,
        ttl_sec=ttl_sec if ttl_sec > 0 else None,
    )
    console.print_json(data=result)


node_task_app = typer.Typer(help="Manage node tasks")
node_app.add_typer(node_task_app, name="task")


@node_task_app.command("add")
def node_task_add(
    node_id: str = typer.Option(..., "--node-id", help="Target node ID"),
    task_type: str = typer.Option(..., "--type", help="Task type"),
    payload: str = typer.Option("{}", "--payload", help="JSON payload"),
    idempotency_key: str = typer.Option(
        "", "--idempotency-key", help="Optional idempotency key for dedup"
    ),
    required_capability: str = typer.Option(
        "",
        "--required-capability",
        help="Optional required node capability (defaults inferred from task type)",
    ),
):
    """Create task for a node."""
    import json

    svc = _node_service()
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")
    except ValueError as e:
        console.print(f"[red]invalid payload:[/red] {e}")
        raise typer.Exit(1)
    task = svc.add_task(
        node_id=node_id,
        task_type=task_type,
        payload=data,
        idempotency_key=idempotency_key,
        required_capability=required_capability,
    )
    if not task:
        console.print(f"[red]node not found:[/red] {node_id}")
        raise typer.Exit(1)
    if task.get("ok") is False and task.get("error_code") == "node_capability_denied":
        console.print(
            "[red]task rejected:[/red] node lacks required capability "
            f"`{task.get('required_capability')}`"
        )
        raise typer.Exit(1)
    if task.get("ok") is False and task.get("error_code") == "node_policy_denied":
        console.print(
            f"[red]task rejected:[/red] task type denied by node policy `{task.get('task_type')}`"
        )
        raise typer.Exit(1)
    if task.get("ok") is False and task.get("error_code") == "node_dsl_static_denied":
        violations = task.get("violations") or []
        v_text = (
            ", ".join([str(v) for v in violations])
            if isinstance(violations, list)
            else str(violations)
        )
        console.print(f"[red]task rejected:[/red] DSL static check failed ({v_text})")
        raise typer.Exit(1)
    if task.get("ok") is False and task.get("error_code") == "node_replay_conflict":
        console.print(
            "[red]task rejected:[/red] idempotency replay conflict "
            f"(key={task.get('idempotency_key')}, existing_task_id={task.get('existing_task_id')})"
        )
        raise typer.Exit(1)
    console.print_json(data=task)


@node_task_app.command("pull")
def node_task_pull(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    token: str = typer.Option(..., "--token", help="Node token"),
):
    """Pull next pending task for a node."""
    svc = _node_service()
    task = svc.pull_task(node_id=node_id, token=token)
    if not task:
        console.print("No pending task (or auth failed).")
        return
    console.print_json(data=task)


@node_task_app.command("ack")
def node_task_ack(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    token: str = typer.Option(..., "--token", help="Node token"),
    task_id: str = typer.Option(..., "--task-id", help="Task ID"),
):
    """Acknowledge task execution start."""
    svc = _node_service()
    ok = svc.ack_task(node_id=node_id, token=token, task_id=task_id)
    if not ok:
        console.print("[red]ack rejected[/red]")
        raise typer.Exit(1)
    console.print("[green]✓[/green] ack accepted")


@node_task_app.command("result")
def node_task_result(
    node_id: str = typer.Option(..., "--node-id", help="Node ID"),
    token: str = typer.Option(..., "--token", help="Node token"),
    task_id: str = typer.Option(..., "--task-id", help="Task ID"),
    ok: bool = typer.Option(True, "--ok/--fail", help="Mark task as success/failure"),
    result: str = typer.Option("{}", "--result", help="JSON result payload"),
    error: str = typer.Option("", "--error", help="Error message when --fail"),
):
    """Submit task execution result."""
    import json

    svc = _node_service()
    try:
        result_data = json.loads(result)
        if not isinstance(result_data, dict):
            raise ValueError("result must be a JSON object")
    except ValueError as e:
        console.print(f"[red]invalid result payload:[/red] {e}")
        raise typer.Exit(1)
    accepted = svc.complete_task(
        node_id=node_id,
        token=token,
        task_id=task_id,
        ok=ok,
        result=result_data,
        error=error,
    )
    if not accepted:
        console.print("[red]result rejected[/red]")
        raise typer.Exit(1)
    console.print("[green]✓[/green] result accepted")


@node_task_app.command("list")
def node_task_list(
    node_id: str = typer.Option("", "--node-id", help="Filter by node ID"),
):
    """List node tasks."""
    svc = _node_service()
    rows = svc.list_tasks(node_id=node_id or None)
    if not rows:
        console.print("No tasks.")
        return
    console.print_json(data=rows)


@node_task_app.command("approve")
def node_task_approve(
    task_id: str = typer.Option(..., "--task-id", help="Task ID"),
    by: str = typer.Option("admin", "--by", help="Approver identity"),
    note: str = typer.Option("", "--note", help="Approval note"),
):
    """Approve a pending_approval task and release it to pending queue."""
    svc = _node_service()
    ok = svc.approve_task(task_id=task_id, approved_by=by, note=note)
    if not ok:
        console.print("[red]approve rejected[/red]")
        raise typer.Exit(1)
    console.print("[green]✓[/green] task approved")


@node_task_app.command("reject")
def node_task_reject(
    task_id: str = typer.Option(..., "--task-id", help="Task ID"),
    by: str = typer.Option("admin", "--by", help="Approver identity"),
    reason: str = typer.Option("", "--reason", help="Rejection reason"),
):
    """Reject a pending task."""
    svc = _node_service()
    ok = svc.reject_task(task_id=task_id, rejected_by=by, reason=reason)
    if not ok:
        console.print("[red]reject failed[/red]")
        raise typer.Exit(1)
    console.print("[green]✓[/green] task rejected")


@node_task_app.command("approvals")
def node_task_approvals(
    node_id: str = typer.Option("", "--node-id", help="Filter by node ID"),
    task_id: str = typer.Option("", "--task-id", help="Filter by task ID"),
):
    """List approval audit events."""
    svc = _node_service()
    rows = svc.list_approval_events(node_id=node_id or None, task_id=task_id or None)
    if not rows:
        console.print("No approval events.")
        return
    console.print_json(data=rows)


@node_task_app.command("approvals-verify")
def node_task_approvals_verify():
    """Verify approval event hash chain and signatures."""
    svc = _node_service()
    result = svc.verify_approval_events()
    console.print_json(data=result)
    if not bool(result.get("ok")):
        raise typer.Exit(1)


@node_task_app.command("approvals-sync-immutable")
def node_task_approvals_sync_immutable():
    """Backfill approval events into immutable audit sink directory."""
    svc = _node_service()
    result = svc.sync_approval_events_to_immutable()
    console.print_json(data=result)
    if not bool(result.get("ok")):
        raise typer.Exit(1)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed status"),
):
    """Show zen-claw status."""
    from zen_claw.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{_display_logo()} zen-claw Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")
        console.print(
            "Vision Model: "
            + (
                config.agents.defaults.vision_model
                if config.agents.defaults.vision_model
                else "(same as model)"
            )
        )
        console.print(f"Memory Recall Mode: {config.agents.defaults.memory_recall_mode}")
        console.print(f"Planning Enabled: {config.agents.defaults.enable_planning}")
        console.print(f"Max Reflections: {config.agents.defaults.max_reflections}")
        console.print(f"Auto Parameter Rewrite: {config.agents.defaults.auto_parameter_rewrite}")
        console.print(f"Skill Permissions Mode: {config.agents.defaults.skill_permissions_mode}")

        # Check API keys
        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        has_zhipu = bool(config.providers.zhipu.api_key)
        has_vllm = bool(config.providers.vllm.api_base)
        has_aihubmix = bool(config.providers.aihubmix.api_key)

        console.print(
            f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}"
        )
        console.print(
            f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}"
        )
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        console.print(f"Zhipu AI API: {'[green]✓[/green]' if has_zhipu else '[dim]not set[/dim]'}")
        console.print(
            f"AiHubMix API: {'[green]✓[/green]' if has_aihubmix else '[dim]not set[/dim]'}"
        )
        vllm_status = (
            f"[green]{config.providers.vllm.api_base}[/green]" if has_vllm else "[dim]not set[/dim]"
        )
        console.print(f"vLLM/Local: {vllm_status}")
        _print_effective_tool_backends(config)
        _print_sidecar_status(config)
        _print_channel_rate_limit_status(config)
        _print_channel_rbac_status(config, verbose=verbose)
        _print_agent_profile_status(config, verbose=verbose)
        _print_node_token_rotation_status(within_sec=3600)
        if verbose:
            _print_policy_audit_matrix(config)


if __name__ == "__main__":
    app()
