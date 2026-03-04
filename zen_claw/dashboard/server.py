"""Lightweight local dashboard server for operational visibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zen_claw.config.schema import Config

try:
    from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    _HAS_FASTAPI = True
except Exception:
    _HAS_FASTAPI = False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _verify_approval_chain(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify approval event hash chain integrity (signature excluded)."""
    prev_hash = ""
    checked = 0
    for idx, row in enumerate(events):
        if not isinstance(row, dict):
            return {"ok": False, "checked": checked, "error_index": idx, "error": "invalid event row"}
        if str(row.get("prev_hash") or "") != prev_hash:
            return {"ok": False, "checked": checked, "error_index": idx, "error": "approval chain broken"}
        base_event = {
            "event_id": str(row.get("event_id") or ""),
            "task_id": str(row.get("task_id") or ""),
            "node_id": str(row.get("node_id") or ""),
            "action": str(row.get("action") or ""),
            "actor": str(row.get("actor") or ""),
            "note": str(row.get("note") or ""),
            "at_ms": int(row.get("at_ms") or 0),
            "prev_hash": str(row.get("prev_hash") or ""),
        }
        canonical = json.dumps(base_event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if str(row.get("hash") or "") != expected_hash:
            return {"ok": False, "checked": checked, "error_index": idx, "error": "approval hash mismatch"}
        prev_hash = expected_hash
        checked += 1
    return {"ok": True, "checked": checked, "error_index": None, "error": ""}


def build_dashboard_snapshot(config: "Config") -> dict[str, Any]:
    """Build dashboard status payload from config and runtime state files."""
    from zen_claw.config.loader import get_data_dir
    from zen_claw.runtime.sidecar_supervisor import collect_sidecar_status

    data_dir = get_data_dir()
    now_ms = int(time.time() * 1000)

    cron_data = _read_json(data_dir / "cron" / "jobs.json")
    cron_jobs = cron_data.get("jobs", []) if isinstance(cron_data.get("jobs"), list) else []
    cron_total = len(cron_jobs)
    cron_enabled = len([j for j in cron_jobs if bool(j.get("enabled", True))])
    cron_failures = len(
        [
            j
            for j in cron_jobs
            if isinstance(j.get("state"), dict) and str(j["state"].get("lastStatus") or "") == "error"
        ]
    )

    def _cron_job_row(job: dict[str, Any]) -> dict[str, Any]:
        state = job.get("state") if isinstance(job.get("state"), dict) else {}
        sched = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        return {
            "id": str(job.get("id") or ""),
            "name": str(job.get("name") or ""),
            "enabled": bool(job.get("enabled", True)),
            "schedule_kind": str(sched.get("kind") or ""),
            "next_run_at_ms": state.get("nextRunAtMs"),
            "last_status": state.get("lastStatus"),
            "last_error": state.get("lastError"),
        }

    cron_rows = [_cron_job_row(j) for j in cron_jobs][:50]

    rate_stats = _read_json(data_dir / "channels" / "rate_limit_stats.json")
    runtime_channels = rate_stats.get("channels", {}) if isinstance(rate_stats, dict) else {}
    rate_runtime_rows: list[dict[str, Any]] = []
    if isinstance(runtime_channels, dict):
        for name, row in sorted(runtime_channels.items()):
            if not isinstance(row, dict):
                continue
            rate_runtime_rows.append(
                {
                    "channel": str(name),
                    "delayed_count": int(row.get("delayed_count", 0)),
                    "dropped_count": int(row.get("dropped_count", 0)),
                    "last_delay_ms": int(row.get("last_delay_ms", 0)),
                }
            )

    channel_rows: list[dict[str, Any]] = []
    for name, ch_cfg in [
        ("telegram", config.channels.telegram),
        ("discord", config.channels.discord),
        ("whatsapp", config.channels.whatsapp),
        ("feishu", config.channels.feishu),
    ]:
        admins = sorted({str(v).strip() for v in getattr(ch_cfg, "admins", []) if str(v).strip()})
        users = sorted({str(v).strip() for v in getattr(ch_cfg, "users", []) if str(v).strip()})
        channel_rows.append(
            {
                "name": name,
                "enabled": bool(getattr(ch_cfg, "enabled", False)),
                "rbac_enabled": bool(admins or users),
                "admins": len(admins),
                "users": len(users),
            }
            )

    node_data = _read_json(data_dir / "nodes" / "state.json")
    nodes = node_data.get("nodes", {}) if isinstance(node_data.get("nodes"), dict) else {}
    tasks = node_data.get("tasks", []) if isinstance(node_data.get("tasks"), list) else []
    approval_events = node_data.get("approval_events", []) if isinstance(node_data.get("approval_events"), list) else []
    approval_chain = _verify_approval_chain([e for e in approval_events if isinstance(e, dict)])

    pending_approval = 0
    pending_approval_overdue = 0
    timeout_rejected = 0
    queue_pending = 0
    queue_running = 0
    queue_failed = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status") or "").strip().lower()
        if status in {"pending", "pending_approval"}:
            queue_pending += 1
        if status in {"leased", "running"}:
            queue_running += 1
        if status in {"rejected", "error"}:
            queue_failed += 1
        if status == "pending_approval":
            pending_approval += 1
            approval = t.get("approval") if isinstance(t.get("approval"), dict) else {}
            expires_at = approval.get("expires_at_ms")
            if isinstance(expires_at, int) and expires_at > 0 and expires_at < now_ms:
                pending_approval_overdue += 1
        if status == "rejected" and str(t.get("error") or "").strip().lower() == "approval timeout":
            timeout_rejected += 1

    latest_task_by_node: dict[str, dict[str, Any]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        node_id = str(t.get("node_id") or "")
        if not node_id:
            continue
        cur = latest_task_by_node.get(node_id)
        cur_ms = int(cur.get("updated_at_ms") or cur.get("created_at_ms") or 0) if isinstance(cur, dict) else -1
        row_ms = int(t.get("updated_at_ms") or t.get("created_at_ms") or 0)
        if row_ms >= cur_ms:
            latest_task_by_node[node_id] = t

    node_rows: list[dict[str, Any]] = []
    for node_id, node in list(nodes.items())[:50]:
        if not isinstance(node, dict):
            continue
        policy = node.get("policy") if isinstance(node.get("policy"), dict) else {}
        latest_task = latest_task_by_node.get(str(node_id), {})
        node_rows.append(
            {
                "node_id": str(node_id),
                "name": str(node.get("name") or ""),
                "platform": str(node.get("platform") or ""),
                "status": str(node.get("status") or ""),
                "last_seen_ms": node.get("last_seen_ms"),
                "allow_gateway_tasks": bool(policy.get("allow_gateway_tasks", True)),
                "max_running_tasks": max(1, int(policy.get("max_running_tasks", 1) or 1)),
                "approval_required_count": max(1, int(policy.get("approval_required_count", 1) or 1)),
                "latest_task_type": str(latest_task.get("task_type") or ""),
                "latest_task_status": str(latest_task.get("status") or ""),
                "latest_task_updated_at_ms": latest_task.get("updated_at_ms"),
            }
        )
    node_rows.sort(key=lambda x: (x.get("name") or "", x.get("node_id") or ""))

    approval_timeline: list[dict[str, Any]] = []
    for e in sorted(
        [r for r in approval_events if isinstance(r, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:30]:
        approval_timeline.append(
            {
                "event_id": str(e.get("event_id") or ""),
                "at_ms": e.get("at_ms"),
                "node_id": str(e.get("node_id") or ""),
                "task_id": str(e.get("task_id") or ""),
                "action": str(e.get("action") or ""),
                "actor": str(e.get("actor") or ""),
                "note": str(e.get("note") or ""),
            }
        )

    provider_rows: list[dict[str, Any]] = []
    compression_events: list[dict[str, Any]] = []
    try:
        sessions_dir = Path.home() / ".zen-claw" / "sessions"
        for sp in sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                first = sp.read_text(encoding="utf-8").splitlines()[0]
                row = json.loads(first)
                meta = row.get("metadata") if isinstance(row, dict) else {}
                events = meta.get("compression_events") if isinstance(meta, dict) else None
                if isinstance(events, list):
                    for ev in events[-5:]:
                        if isinstance(ev, dict):
                            compression_events.append(
                                {
                                    "session": sp.stem,
                                    "at_ms": ev.get("at_ms"),
                                    "at_turn": ev.get("at_turn"),
                                    "reason": ev.get("reason"),
                                }
                            )
            except Exception:
                continue
    except Exception:
        compression_events = []
    compression_events = sorted(
        compression_events,
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:30]

    for name in [
        "openrouter",
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "zhipu",
        "dashscope",
        "moonshot",
        "groq",
        "aihubmix",
        "vllm",
    ]:
        p = getattr(config.providers, name)
        provider_rows.append(
            {
                "name": name,
                "api_key_set": bool(str(p.api_key or "").strip()),
            }
        )

    return {
        "generated_at_ms": now_ms,
        "agent": {
            "model": config.agents.defaults.model,
            "vision_model": config.agents.defaults.vision_model or "",
            "memory_recall_mode": config.agents.defaults.memory_recall_mode,
            "planning_enabled": bool(config.agents.defaults.enable_planning),
            "max_reflections": int(config.agents.defaults.max_reflections),
            "compression_trigger_ratio": float(config.agents.defaults.compression_trigger_ratio),
            "compression_hysteresis_ratio": float(config.agents.defaults.compression_hysteresis_ratio),
            "compression_cooldown_turns": int(config.agents.defaults.compression_cooldown_turns),
            "compression_events": compression_events,
        },
        "security": {
            "production_hardening": bool(config.tools.policy.production_hardening),
            "subagent_hard_guardrail": not bool(config.tools.policy.allow_subagent_sensitive_tools),
            "skill_permissions_mode": str(config.agents.defaults.skill_permissions_mode),
        },
        "providers": provider_rows,
        "sidecars": collect_sidecar_status(config),
        "cron": {
            "total_jobs": cron_total,
            "enabled_jobs": cron_enabled,
            "failed_jobs": cron_failures,
            "jobs": cron_rows,
        },
        "channels": channel_rows,
        "rate_limit": {
            "default": {
                "mode": config.channels.outbound_rate_limit_mode,
                "per_sec": float(config.channels.outbound_rate_limit_per_sec),
                "burst": int(config.channels.outbound_rate_limit_burst),
            },
            "runtime": rate_runtime_rows,
        },
        "node": {
            "total_nodes": len(nodes),
            "active_nodes": len(
                [
                    n
                    for n in nodes.values()
                    if isinstance(n, dict) and str(n.get("status") or "").strip().lower() == "active"
                ]
            ),
            "total_tasks": len([t for t in tasks if isinstance(t, dict)]),
            "queue_pending": queue_pending,
            "queue_running": queue_running,
            "queue_failed": queue_failed,
            "pending_approval": pending_approval,
            "pending_approval_overdue": pending_approval_overdue,
            "approval_timeout_rejected": timeout_rejected,
            "approval_events": len([e for e in approval_events if isinstance(e, dict)]),
            "approval_chain_ok": bool(approval_chain.get("ok")),
            "approval_chain_checked": int(approval_chain.get("checked") or 0),
            "approval_chain_error": str(approval_chain.get("error") or ""),
            "nodes": node_rows,
            "approval_timeline": approval_timeline,
        },
    }


def _api_keys_file() -> Path:
    from zen_claw.config.loader import get_data_dir

    return get_data_dir() / "api_keys.json"


def _load_api_keys() -> dict[str, dict]:
    path = _api_keys_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_api_keys(keys: dict[str, dict]) -> None:
    path = _api_keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    raw = "nc-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return raw, raw[:12]


def store_api_key(raw: str) -> str:
    keys = _load_api_keys()
    hashed = _hash_key(raw)
    prefix = raw[:12]
    keys[hashed] = {"prefix": prefix, "created_at": int(time.time()), "enabled": True}
    _save_api_keys(keys)
    return prefix


def verify_api_key(raw: str) -> bool:
    hashed = _hash_key(raw)
    env_keys = str(os.environ.get("zen_claw_API_KEYS", "")).strip()
    if env_keys:
        for key in env_keys.split(","):
            token = key.strip()
            if token and _hash_key(token) == hashed:
                return True
    entry = _load_api_keys().get(hashed)
    return bool(entry and entry.get("enabled", True))


def revoke_api_key_by_prefix(prefix: str) -> bool:
    keys = _load_api_keys()
    found = False
    for _, row in keys.items():
        p = str(row.get("prefix", ""))
        if p.startswith(prefix):
            row["enabled"] = False
            found = True
    if found:
        _save_api_keys(keys)
    return found


async def _invoke_agent_text(message: str, session_id: str) -> str:
    # Graceful fallback keeps API usable in unconfigured/dev environments.
    try:
        from zen_claw.agent.loop import AgentLoop
        from zen_claw.bus.queue import MessageBus
        from zen_claw.config.loader import load_config
        from zen_claw.providers.litellm_provider import LiteLLMProvider

        cfg = load_config()
        provider_cfg = cfg.get_provider(cfg.agents.defaults.model)
        if not provider_cfg or not provider_cfg.api_key:
            return f"[echo] {message}"
        provider = LiteLLMProvider(
            api_key=provider_cfg.api_key,
            api_base=cfg.get_api_base(cfg.agents.defaults.model),
            default_model=cfg.agents.defaults.model,
            extra_headers=provider_cfg.extra_headers,
            rate_limit_delay_sec=provider_cfg.rate_limit_delay_sec,
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=cfg.workspace_path,
            model=cfg.agents.defaults.model,
            max_iterations=cfg.agents.defaults.max_tool_iterations,
            memory_recall_mode=cfg.agents.defaults.memory_recall_mode,
            enable_planning=cfg.agents.defaults.enable_planning,
            max_reflections=cfg.agents.defaults.max_reflections,
            auto_parameter_rewrite=cfg.agents.defaults.auto_parameter_rewrite,
            max_context_tokens=cfg.agents.defaults.max_tokens,
            compression_trigger_ratio=cfg.agents.defaults.compression_trigger_ratio,
            compression_hysteresis_ratio=cfg.agents.defaults.compression_hysteresis_ratio,
            compression_cooldown_turns=cfg.agents.defaults.compression_cooldown_turns,
            vision_model=cfg.agents.defaults.vision_model or None,
            skill_permissions_mode=cfg.agents.defaults.skill_permissions_mode,
            allowed_models=cfg.agents.defaults.allowed_models,
        )
        return await loop.process_direct(content=message, session_key=f"web:{session_id}", channel="web_chat", chat_id=session_id)
    except Exception:
        return f"[echo] {message}"


class _WebChatRuntime:
    """Lazy in-process webchat runtime backed by message bus + agent loop."""

    def __init__(self, cfg: "Config"):
        from zen_claw.agent.loop import AgentLoop
        from zen_claw.bus.queue import MessageBus
        from zen_claw.channels.webchat import WebChatChannel
        from zen_claw.providers.litellm_provider import LiteLLMProvider

        provider_cfg = cfg.get_provider(cfg.agents.defaults.model)
        if not provider_cfg or not provider_cfg.api_key:
            raise RuntimeError("provider_not_configured")
        self.cfg = cfg
        self.bus = MessageBus()
        self.channel = WebChatChannel(cfg.channels.webchat, self.bus, media_root=cfg.workspace_path / "media")
        self.channel.access_checker = lambda *_args, **_kwargs: True
        self.provider = LiteLLMProvider(
            api_key=provider_cfg.api_key,
            api_base=cfg.get_api_base(cfg.agents.defaults.model),
            default_model=cfg.agents.defaults.model,
            extra_headers=provider_cfg.extra_headers,
            rate_limit_delay_sec=provider_cfg.rate_limit_delay_sec,
        )
        self.agent = AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=cfg.workspace_path,
            model=cfg.agents.defaults.model,
            max_iterations=cfg.agents.defaults.max_tool_iterations,
            memory_recall_mode=cfg.agents.defaults.memory_recall_mode,
            enable_planning=cfg.agents.defaults.enable_planning,
            max_reflections=cfg.agents.defaults.max_reflections,
            auto_parameter_rewrite=cfg.agents.defaults.auto_parameter_rewrite,
            max_context_tokens=cfg.agents.defaults.max_tokens,
            compression_trigger_ratio=cfg.agents.defaults.compression_trigger_ratio,
            compression_hysteresis_ratio=cfg.agents.defaults.compression_hysteresis_ratio,
            compression_cooldown_turns=cfg.agents.defaults.compression_cooldown_turns,
            vision_model=cfg.agents.defaults.vision_model or None,
            skill_permissions_mode=cfg.agents.defaults.skill_permissions_mode,
            allowed_models=cfg.agents.defaults.allowed_models,
        )
        self._dispatcher_task: asyncio.Task | None = None
        self._agent_task: asyncio.Task | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.channel.start()
        self._dispatcher_task = asyncio.create_task(self._dispatch_outbound())
        self._agent_task = asyncio.create_task(self.agent.run())

    async def _dispatch_outbound(self) -> None:
        while True:
            try:
                msg = await self.bus.consume_outbound()
                if msg.channel == "webchat":
                    await self.channel.send(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                continue


_WEBCHAT_RUNTIME_BY_LOOP: dict[int, _WebChatRuntime] = {}
_WEBCHAT_RUNTIME_LOCK: asyncio.Lock | None = None


async def _get_webchat_runtime() -> _WebChatRuntime | None:
    from zen_claw.config.loader import load_config

    global _WEBCHAT_RUNTIME_LOCK
    if _WEBCHAT_RUNTIME_LOCK is None:
        _WEBCHAT_RUNTIME_LOCK = asyncio.Lock()
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    if loop_id in _WEBCHAT_RUNTIME_BY_LOOP:
        return _WEBCHAT_RUNTIME_BY_LOOP[loop_id]
    async with _WEBCHAT_RUNTIME_LOCK:
        if loop_id in _WEBCHAT_RUNTIME_BY_LOOP:
            return _WEBCHAT_RUNTIME_BY_LOOP[loop_id]
        try:
            runtime = _WebChatRuntime(load_config())
            await runtime.start()
            _WEBCHAT_RUNTIME_BY_LOOP[loop_id] = runtime
            return runtime
        except Exception:
            return None


if _HAS_FASTAPI:
    import collections

    from pydantic import BaseModel, Field

    # ── Pydantic schemas ──────────────────────────────────────────────────────

    class InvokeRequest(BaseModel):
        message: str = Field(..., description="The user message to send to the agent.", example="帮我查一下今天的天气")
        session_id: str | None = Field(None, description="Optional session ID for conversation continuity. Auto-generated if omitted.", example="abc-123")

    class InvokeResponse(BaseModel):
        response: str = Field(..., description="The agent's reply.")
        session_id: str = Field(..., description="Session ID used for this turn.")

    class HealthResponse(BaseModel):
        status: str = Field(..., example="ok")
        service: str = Field(..., example="zen-claw")

    class InfoResponse(BaseModel):
        version: str
        model: str
        capabilities: list[str]

    # ── Middleware: API Key auth ───────────────────────────────────────────────

    _CHAT_HTML_PATH = Path(__file__).parent / "static" / "chat.html"

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        EXEMPT = {"/api/v1/health", "/chat", "/chat/upload"}

        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/v1/") and path not in self.EXEMPT:
                api_key = str(request.headers.get("X-API-Key", ""))
                if not api_key or not verify_api_key(api_key):
                    return JSONResponse(status_code=401, content={"error": "invalid_api_key", "detail": "Provide a valid API key via the X-API-Key header."})
            return await call_next(request)

    # ── Middleware: Simple sliding-window rate limiter ────────────────────────

    class RateLimitMiddleware(BaseHTTPMiddleware):
        """Token-bucket rate limiter: max N requests per window per IP (no extra deps)."""

        def __init__(self, app, max_requests: int = 60, window_sec: int = 60):
            super().__init__(app)
            self._max = max_requests
            self._window = window_sec
            # {ip: deque[timestamp]}
            self._buckets: dict[str, collections.deque] = collections.defaultdict(collections.deque)

        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith("/api/v1/agent/"):
                return await call_next(request)
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            bucket = self._buckets[ip]
            # Evict timestamps outside the window
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= self._max:
                return JSONResponse(
                    status_code=429,
                    content={"error": "rate_limit_exceeded", "detail": f"Max {self._max} requests per {self._window}s."},
                    headers={"Retry-After": str(self._window)},
                )
            bucket.append(now)
            return await call_next(request)

    # ── FastAPI app ───────────────────────────────────────────────────────────

    _API_DESCRIPTION = """
## zen-claw REST API

轻量级本地 AI 代理引擎，支持工具调用、长期记忆、多渠道集成。

### 鉴权方式
所有 `/api/v1/` 端点（`/health` 除外）需在请求头携带 API Key：

```
X-API-Key: nc-xxxxxxxxxxxxxxxx
```

通过 CLI 生成密钥：`zen-claw api-key generate`

### 限流策略
调用类端点（`/agent/invoke*`）：每 IP 每 60 秒最多 60 次请求。  
超限返回 **HTTP 429**，`Retry-After` 头指示等待秒数。
"""

    api_app = FastAPI(
        title="zen-claw API",
        version="1.0.0",
        description=_API_DESCRIPTION,
        contact={"name": "zen-claw", "url": "https://github.com/ZachCharles666/zen-claw-public"},
        license_info={"name": "MIT"},
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        openapi_tags=[
            {"name": "agent", "description": "向 AI 代理发送消息并获取回复"},
            {"name": "system", "description": "服务健康检查与版本信息"},
            {"name": "chat", "description": "浏览器端聊天界面及文件上传"},
        ],
    )
    api_app.add_middleware(RateLimitMiddleware, max_requests=60, window_sec=60)
    api_app.add_middleware(ApiKeyMiddleware)
    api_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    try:
        from zen_claw.auth.middleware import MultiTenantAuthMiddleware
        from zen_claw.auth.session import SessionManager
        from zen_claw.config.loader import load_config

        _mt_config = load_config().multitenant
        if _mt_config.enabled and _mt_config.jwt_secret.get_secret_value():
            _session_mgr = SessionManager(
                secret=_mt_config.jwt_secret.get_secret_value(),
                algorithm=_mt_config.jwt_algorithm,
                expire_seconds=_mt_config.jwt_expire_seconds,
            )
            api_app.add_middleware(
                MultiTenantAuthMiddleware,
                session_manager=_session_mgr,
                public_paths=_mt_config.public_paths,
                login_path=_mt_config.login_path,
                cookie_name=_mt_config.session_cookie_name,
            )
    except Exception:
        pass
    try:
        from zen_claw.dashboard.webhooks import webhook_router

        if webhook_router is not None:
            api_app.include_router(webhook_router)
    except Exception:
        pass

    @api_app.get("/chat", response_class=HTMLResponse)
    async def chat_ui():
        if _CHAT_HTML_PATH.exists():
            return HTMLResponse(_CHAT_HTML_PATH.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="chat ui not found")

    _LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Login</title>
<style>
body{font-family:'Segoe UI','PingFang SC',sans-serif;background:#f3f6fb;margin:0;display:grid;place-items:center;min-height:100vh}
.box{background:#fff;border:1px solid #d9e1ec;border-radius:12px;padding:20px;min-width:320px}
input{width:100%;margin-top:8px;padding:8px;border:1px solid #c9d6e7;border-radius:8px}
button{margin-top:12px;width:100%;padding:10px;border:none;border-radius:8px;background:#0d6efd;color:#fff}
.err{color:#b42318;margin-top:8px}
</style></head>
<body><div class="box"><h3>zen-claw 登录</h3>{error_html}
<form method="post" action="/login"><input type="hidden" name="next" value="{next_url}">
<input name="username" placeholder="用户名"><input type="password" name="password" placeholder="密码">
<button type="submit">登录</button></form></div></body></html>"""

    @api_app.get("/login", response_class=HTMLResponse)
    async def login_page(next: str = "/", error: str = ""):
        err = f'<div class="err">{error}</div>' if error else ""
        return HTMLResponse(_LOGIN_HTML.format(error_html=err, next_url=next))

    @api_app.post("/login")
    async def login_submit(request: Request):
        from fastapi.responses import RedirectResponse

        from zen_claw.auth.session import SessionManager
        from zen_claw.auth.user import UserStore
        from zen_claw.config.loader import get_data_dir, load_config

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_url = str(form.get("next", "/"))

        cfg = load_config()
        user_store = UserStore(get_data_dir())
        user = user_store.authenticate(username=username, password=password)
        if not user:
            return HTMLResponse(_LOGIN_HTML.format(error_html='<div class="err">用户名或密码错误</div>', next_url=next_url), status_code=401)

        session_mgr = SessionManager(
            secret=cfg.multitenant.jwt_secret.get_secret_value(),
            algorithm=cfg.multitenant.jwt_algorithm,
            expire_seconds=cfg.multitenant.jwt_expire_seconds,
        )
        token = session_mgr.create_session(user.user_id, user.tenant_id, user.username, user.role)
        response = RedirectResponse(url=next_url if next_url.startswith("/") else "/", status_code=302)
        response.set_cookie(
            key=cfg.multitenant.session_cookie_name,
            value=token,
            httponly=True,
            secure=cfg.multitenant.session_cookie_secure,
            samesite="lax",
            max_age=cfg.multitenant.jwt_expire_seconds,
        )
        return response

    @api_app.get("/logout")
    async def logout():
        from fastapi.responses import RedirectResponse

        from zen_claw.config.loader import load_config

        cfg = load_config()
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key=cfg.multitenant.session_cookie_name)
        return response

    @api_app.websocket("/chat/ws/{session_id}")
    async def chat_ws(websocket: WebSocket, session_id: str):
        runtime = await _get_webchat_runtime()
        if runtime is not None:
            required_token = str(runtime.cfg.channels.webchat.token or "").strip()
            if required_token:
                provided = str(websocket.query_params.get("token") or "").strip()
                if provided != required_token:
                    await websocket.close(code=1008, reason="unauthorized")
                    return
        await websocket.accept()
        try:
            while True:
                payload = json.loads(await websocket.receive_text())
                if payload.get("type") != "message":
                    continue
                content = str(payload.get("content", "")).strip()
                if not content:
                    continue
                if runtime is not None:
                    sender_id = str(payload.get("sender_id") or "webchat_user")
                    media = payload.get("media")
                    media_list = media if isinstance(media, list) else None
                    await runtime.channel.ingest_user_message(
                        session_id=session_id,
                        sender_id=sender_id,
                        content=content,
                        media=media_list,
                        metadata={"session_key": f"webchat:{session_id}"},
                    )
                    out = await runtime.channel.pop_response(session_id, timeout_sec=120.0)
                    if out is None:
                        raise RuntimeError("webchat_response_timeout")
                    answer = out.content
                else:
                    answer = await _invoke_agent_text(content, session_id)
                for ch in answer:
                    await websocket.send_text(json.dumps({"type": "token", "content": ch}))
                await websocket.send_text(json.dumps({"type": "done"}))
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))

    @api_app.post("/chat/upload")
    async def chat_upload(request: Request):
        from zen_claw.config.loader import get_data_dir

        upload_dir = get_data_dir() / "uploads" / "chat"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        saved_paths: list[str] = []
        try:
            form = await request.form()
            files = form.getlist("files")
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "saved": [],
                    "media_paths": [],
                    "rag_status": "upload_parse_failed",
                    "message": "invalid upload payload",
                },
            )
        for f in files:
            filename = getattr(f, "filename", None)
            reader = getattr(f, "read", None)
            if not filename or not callable(reader):
                continue
            name = Path(str(filename)).name
            stamp = uuid.uuid4().hex[:8]
            dest = upload_dir / f"{stamp}_{name}"
            dest.write_bytes(await reader())
            saved.append(dest.name)
            saved_paths.append(str(dest))
        rag_status = "rag_not_installed"
        try:
            from zen_claw.agent.tools.knowledge import KnowledgeAddTool

            tool = KnowledgeAddTool(data_dir=get_data_dir())
            for file_path in saved_paths:
                await tool.execute(source=file_path, notebook_id="chat_uploads")
            rag_status = "ingested"
        except Exception:
            rag_status = "rag_not_available"
        return JSONResponse(
            {
                "saved": saved,
                "media_paths": saved_paths,
                "rag_status": rag_status,
                "message": f"{len(saved)} file(s) uploaded. RAG status: {rag_status}.",
            }
        )

    api_router = APIRouter(prefix="/api/v1")

    @api_router.get(
        "/health",
        summary="健康检查",
        description="返回服务运行状态，无需鉴权。可用于负载均衡器或监控系统的存活探针。",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def api_health():
        return HealthResponse(status="ok", service="zen-claw")

    @api_router.get(
        "/info",
        summary="服务信息",
        description="返回当前部署版本、默认模型以及已启用的能力列表。",
        response_model=InfoResponse,
        tags=["system"],
    )
    async def api_info():
        from zen_claw import __version__
        from zen_claw.config.loader import load_config

        cfg = load_config()
        return InfoResponse(
            version=__version__,
            model=cfg.agents.defaults.model,
            capabilities=["text", "tools", "memory"],
        )

    @api_router.post(
        "/agent/invoke",
        summary="同步调用代理",
        description=(
            "向 AI 代理发送一条消息，等待完整回复后返回。\n\n"
            "- 需要 `X-API-Key` 请求头\n"
            "- 传入相同 `session_id` 可保持多轮对话上下文\n"
            "- 如需流式响应，请使用 `/agent/invoke/stream`"
        ),
        response_model=InvokeResponse,
        responses={
            400: {"description": "消息内容为空", "content": {"application/json": {"example": {"error": "message_required"}}}},
            401: {"description": "API Key 无效或缺失"},
            429: {"description": "请求过于频繁，已触发限流"},
        },
        tags=["agent"],
    )
    async def api_invoke(req: InvokeRequest):
        if not req.message.strip():
            return JSONResponse(status_code=400, content={"error": "message_required"})
        session_id = str(req.session_id or uuid.uuid4())
        text = await _invoke_agent_text(req.message, session_id)
        return InvokeResponse(response=text, session_id=session_id)

    @api_router.post(
        "/agent/invoke/stream",
        summary="流式调用代理 (SSE)",
        description=(
            "向 AI 代理发送消息，以 **Server-Sent Events (SSE)** 格式实时流式返回 token。\n\n"
            "每个事件格式为 `data: {\"token\": \"...\"}\\n\\n`，结束时发送 `data: [DONE]\\n\\n`。\n\n"
            "适用于需要实时打字机效果的前端场景。"
        ),
        responses={
            200: {"description": "SSE 流，Content-Type: text/event-stream"},
            400: {"description": "消息内容为空"},
            401: {"description": "API Key 无效或缺失"},
            429: {"description": "请求过于频繁，已触发限流"},
        },
        tags=["agent"],
    )
    async def api_invoke_stream(req: InvokeRequest):
        if not req.message.strip():
            return JSONResponse(status_code=400, content={"error": "message_required"})
        session_id = str(req.session_id or uuid.uuid4())
        text = await _invoke_agent_text(req.message, session_id)

        async def _gen():
            for ch in text:
                yield f"data: {json.dumps({'token': ch}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    api_app.include_router(api_router)
else:
    api_app = None


def trigger_cron_job_with_audit(job_id: str, *, data_dir: Path) -> dict[str, Any]:
    """Trigger one cron job and append a dashboard audit event."""
    from zen_claw.cron.service import CronService

    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    cron_store = data_dir / "cron" / "jobs.json"
    service = CronService(cron_store)
    ok = asyncio.run(service.run_job(job_id, force=True))

    dashboard_dir = data_dir / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    audit_file = dashboard_dir / "audit.log.jsonl"
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "dashboard.cron.run",
        "job_id": job_id,
        "ok": bool(ok),
    }
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return {"ok": bool(ok), "job_id": job_id, "trace_id": trace_id}


def _render_html(snapshot: dict[str, Any], refresh_sec: int) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>zen-claw Dashboard</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #102136;
      --muted: #526173;
      --accent: #0d6efd;
      --border: #d9e0ea;
    }}
    body {{ margin: 0; font-family: "Segoe UI", "PingFang SC", sans-serif; background: var(--bg); color: var(--ink); }}
    .wrap {{ max-width: 1100px; margin: 24px auto; padding: 0 16px; }}
    .hero {{ margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }}
    .grid-wide {{ display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--border); text-align: left; padding: 4px 6px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    h2 {{ margin: 0 0 6px; font-size: 16px; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    pre {{ margin: 0; max-height: 430px; overflow: auto; font-size: 12px; background: #f1f4f8; padding: 10px; border-radius: 8px; }}
    .ok {{ color: #087f23; }}
    .warn {{ color: #b26a00; }}
    .bad {{ color: #b42318; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>zen-claw Dashboard</h1>
      <div class="muted">Read-only operational view. Auto refresh every {refresh_sec}s.</div>
    </div>
    <div class="grid">
      <div class="card"><h2>Agent</h2><div id="agent"></div></div>
      <div class="card"><h2>Cron</h2><div id="cron"></div></div>
      <div class="card"><h2>Security</h2><div id="security"></div></div>
      <div class="card"><h2>Sidecars</h2><div id="sidecars"></div></div>
      <div class="card"><h2>Node Queue</h2><div id="node"></div></div>
    </div>
    <div class="grid-wide">
      <div class="card"><h2>Node Details</h2><div id="nodeDetails"></div></div>
      <div class="card"><h2>Approval Timeline</h2><div id="approvalTimeline"></div></div>
    </div>
    <div class="card" style="margin-top: 12px;">
      <h2>Raw Snapshot</h2>
      <pre id="raw">{escape(payload)}</pre>
    </div>
  </div>
  <script>
    const refreshMs = {max(refresh_sec, 1) * 1000};
    function p(id, html) {{ document.getElementById(id).innerHTML = html; }}
    function badge(v) {{
      if (v === true) return '<span class="ok">true</span>';
      if (v === false) return '<span class="bad">false</span>';
      return String(v);
    }}
    function fmtAgo(ms, now) {{
      if (!ms) return '-';
      const sec = Math.max(0, Math.floor((now - Number(ms)) / 1000));
      if (sec < 60) return sec + 's ago';
      if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
      if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
      return Math.floor(sec / 86400) + 'd ago';
    }}
    function renderNodeDetails(data) {{
      const now = Date.now();
      const rows = (data.node.nodes || []).slice(0, 20).map(n => `
        <tr>
          <td>${{n.name || '-'}}</td>
          <td>${{n.platform || '-'}}</td>
          <td>${{n.status || '-'}}</td>
          <td>${{fmtAgo(n.last_seen_ms, now)}}</td>
          <td>${{n.allow_gateway_tasks ? 'yes' : 'no'}} / ${{n.max_running_tasks}}</td>
          <td>${{n.approval_required_count}}</td>
          <td>${{n.latest_task_type || '-'}} / ${{n.latest_task_status || '-'}}</td>
        </tr>`).join('');
      p("nodeDetails", `<table>
        <thead><tr><th>Node</th><th>Platform</th><th>Status</th><th>Last Seen</th><th>Gateway/MaxRun</th><th>Approvals</th><th>Latest Task</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="7" class="muted">No nodes</td></tr>'}}</tbody>
      </table>`);
    }}
    function renderApprovalTimeline(data) {{
      const now = Date.now();
      const rows = (data.node.approval_timeline || []).slice(0, 20).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, now)}}</td>
          <td>${{e.action || '-'}}</td>
          <td>${{e.node_id || '-'}}</td>
          <td>${{e.task_id || '-'}}</td>
          <td>${{e.actor || '-'}}</td>
          <td>${{e.note || ''}}</td>
        </tr>`).join('');
      p("approvalTimeline", `<table>
        <thead><tr><th>When</th><th>Action</th><th>Node</th><th>Task</th><th>Actor</th><th>Note</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="6" class="muted">No approval events</td></tr>'}}</tbody>
      </table>`);
    }}
    function render(data) {{
      p(
        "agent",
        `model: <b>${{data.agent.model}}</b><br/>planning: ${{badge(data.agent.planning_enabled)}}<br/>memory: ${{data.agent.memory_recall_mode}}<br/>compress: trigger=${{data.agent.compression_trigger_ratio}} / hysteresis=${{data.agent.compression_hysteresis_ratio}} / cooldown=${{data.agent.compression_cooldown_turns}}<br/>compress events: <b>${{(data.agent.compression_events || []).length}}</b>`
      );
      p("cron", `total: <b>${{data.cron.total_jobs}}</b><br/>enabled: <b>${{data.cron.enabled_jobs}}</b><br/>failed: <span class="${{data.cron.failed_jobs > 0 ? 'bad' : 'ok'}}">${{data.cron.failed_jobs}}</span>`);
      p("security", `hardening: ${{badge(data.security.production_hardening)}}<br/>subagent guardrail: ${{badge(data.security.subagent_hard_guardrail)}}<br/>skill perms: <b>${{data.security.skill_permissions_mode}}</b>`);
      const running = (data.sidecars || []).filter(x => x.status === "running").length;
      p("sidecars", `running: <b>${{running}}</b> / ${{(data.sidecars || []).length}}<br/>details in raw snapshot`);
      const chainClass = data.node.approval_chain_ok ? 'ok' : 'bad';
      const chainText = data.node.approval_chain_ok ? 'ok' : (data.node.approval_chain_error || 'failed');
      p("node", `nodes: <b>${{data.node.active_nodes}}</b>/${{data.node.total_nodes}}<br/>queue pending/running/failed: <b>${{data.node.queue_pending}}</b> / <b>${{data.node.queue_running}}</b> / <b>${{data.node.queue_failed}}</b><br/>approvals pending/overdue: <b>${{data.node.pending_approval}}</b> / <span class="${{data.node.pending_approval_overdue > 0 ? 'bad' : 'ok'}}">${{data.node.pending_approval_overdue}}</span><br/>approval timeout rejected: <b>${{data.node.approval_timeout_rejected}}</b><br/>approval chain: <span class="${{chainClass}}">${{chainText}}</span> (checked=${{data.node.approval_chain_checked}})`);
      renderNodeDetails(data);
      renderApprovalTimeline(data);
      document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
    }}
    async function tick() {{
      try {{
        const r = await fetch('/api/status', {{ cache: 'no-store' }});
        if (!r.ok) throw new Error('http ' + r.status);
        const data = await r.json();
        render(data);
      }} catch (e) {{
        p("agent", '<span class="bad">fetch failed</span>');
      }}
    }}
    setInterval(tick, refreshMs);
    tick();
  </script>
</body>
</html>"""


def run_dashboard_server(
    config: "Config",
    *,
    host: str = "127.0.0.1",
    port: int = 18791,
    refresh_sec: int = 5,
) -> None:
    """Run blocking dashboard server."""
    from zen_claw.config.loader import get_data_dir

    data_dir = get_data_dir()
    _dashboard_token = str(os.environ.get("zen_claw_DASHBOARD_TOKEN", "")).strip()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                payload = json.dumps(build_dashboard_snapshot(config), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path in {"/", "/index.html"}:
                html = _render_html(build_dashboard_snapshot(config), refresh_sec=refresh_sec).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/api/cron/run/"):
                if _dashboard_token:
                    provided_token = str(self.headers.get("X-zen-claw-Token") or "").strip()
                    if provided_token != _dashboard_token:
                        body = json.dumps(
                            {
                                "ok": False,
                                "error": "unauthorized: invalid or missing X-zen-claw-Token header",
                                "error_code": "dashboard_auth_failed",
                            }
                        ).encode("utf-8")
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                confirm = str(self.headers.get("X-zen-claw-Confirm") or "").strip().lower()
                if confirm != "run":
                    body = json.dumps(
                        {
                            "ok": False,
                            "error": "missing confirmation header: X-zen-claw-Confirm: run",
                        }
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                job_id = self.path.rsplit("/", 1)[-1].strip()
                if not job_id:
                    body = b'{"ok":false,"error":"job_id required"}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                result = trigger_cron_job_with_audit(job_id, data_dir=data_dir)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if result["ok"] else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
