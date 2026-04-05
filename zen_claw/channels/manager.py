"""Channel manager for coordinating chat channels."""

import asyncio
import importlib
import json
import time
from typing import Any

from loguru import logger

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel
from zen_claw.channels.outbound_adapter import ChannelOutboundAdapter
from zen_claw.channels.registry import get_channel_config, iter_channel_specs
from zen_claw.channels.routing import AgentRouteStore
from zen_claw.config.schema import Config
from zen_claw.observability.audit import AuditWorker
from zen_claw.observability.trace import TraceContext


class _TokenBucketRateLimiter:
    """Simple async token bucket rate limiter."""

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate_per_sec = float(rate_per_sec)
        self.burst = max(1, int(burst))
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, key: str) -> tuple[bool, float]:
        """Try to acquire one token; return (ok, retry_after_seconds)."""
        if self.rate_per_sec <= 0:
            return True, 0.0
        async with self._lock:
            now = time.monotonic()
            tokens, updated_at = self._state.get(key, (float(self.burst), now))
            elapsed = max(0.0, now - updated_at)
            tokens = min(float(self.burst), tokens + elapsed * self.rate_per_sec)
            if tokens >= 1.0:
                tokens -= 1.0
                self._state[key] = (tokens, now)
                return True, 0.0
            retry_after = (1.0 - tokens) / self.rate_per_sec
            self._state[key] = (tokens, now)
            return False, retry_after

    async def acquire(self, key: str) -> float:
        """Acquire one token and return the waited seconds."""
        if self.rate_per_sec <= 0:
            return 0.0
        waited_total = 0.0
        while True:
            ok, wait_sec = await self.try_acquire(key)
            if ok:
                return waited_total
            waited_total += wait_sec
            await asyncio.sleep(wait_sec)


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        rate_stats_path: Any | None = None,
        audit_worker: AuditWorker | None = None,
    ):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._audit_worker = audit_worker
        self._outbound_adapter = ChannelOutboundAdapter(config)
        self._dispatch_task: asyncio.Task | None = None
        self._route_store = self._init_route_store()
        self._route_gc_ttl_ms = 24 * 60 * 60 * 1000
        self._route_gc_last_ms = 0
        if rate_stats_path is not None:
            self._rate_stats_path = rate_stats_path
        else:
            self._rate_stats_path = self._resolve_default_rate_stats_path()
        self._rate_stats = self._load_rate_stats()
        self._default_rate_limiter = _TokenBucketRateLimiter(
            rate_per_sec=self.config.channels.outbound_rate_limit_per_sec,
            burst=self.config.channels.outbound_rate_limit_burst,
        )
        self._default_rate_limit_mode = (
            str(self.config.channels.outbound_rate_limit_mode or "delay").strip().lower()
        )
        self._channel_rate_limiter: dict[str, _TokenBucketRateLimiter] = {}
        self._channel_rate_limit_mode: dict[str, str] = {}
        for ch, cfg in self.config.channels.outbound_rate_limit_by_channel.items():
            rate = (
                float(cfg.per_sec)
                if cfg.per_sec is not None
                else float(self.config.channels.outbound_rate_limit_per_sec)
            )
            burst = (
                int(cfg.burst)
                if cfg.burst is not None
                else int(self.config.channels.outbound_rate_limit_burst)
            )
            mode = (
                str(cfg.mode).strip().lower()
                if cfg.mode is not None
                else self._default_rate_limit_mode
            )
            self._channel_rate_limiter[ch] = _TokenBucketRateLimiter(rate_per_sec=rate, burst=burst)
            self._channel_rate_limit_mode[ch] = mode
        self._drop_notice_enabled = bool(self.config.channels.outbound_rate_limit_drop_notice)
        self._drop_notice_cooldown_sec = max(
            1, int(self.config.channels.outbound_rate_limit_drop_notice_cooldown_sec)
        )
        self._drop_notice_text = str(
            self.config.channels.outbound_rate_limit_drop_notice_text or ""
        ).strip()
        self._last_drop_notice_at: dict[str, float] = {}

        self._init_channels()

    def _init_route_store(self) -> AgentRouteStore | None:
        from zen_claw.config.loader import get_data_dir

        candidates = []
        candidates.append(self.config.workspace_path / ".zen-claw" / "channels" / "agent_routes.db")
        try:
            candidates.append(get_data_dir() / "channels" / "agent_routes.db")
        except Exception:
            pass
        last_err = None
        for db_path in candidates:
            try:
                return AgentRouteStore(db_path)
            except Exception as e:
                last_err = e
                continue
        logger.warning(f"multi-agent route store disabled: {last_err}")
        return None

    def _resolve_default_rate_stats_path(self):
        from zen_claw.config.loader import get_data_dir

        try:
            return get_data_dir() / "channels" / "rate_limit_stats.json"
        except Exception:
            # Fallback to workspace-local runtime state path when HOME is unavailable.
            return self.config.workspace_path / ".zen-claw" / "channels" / "rate_limit_stats.json"

    def _load_rate_stats(self) -> dict[str, Any]:
        if not self._rate_stats_path.exists():
            return {"updated_at_unix": 0, "channels": {}}
        try:
            data = json.loads(self._rate_stats_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"updated_at_unix": 0, "channels": {}}
            if not isinstance(data.get("channels"), dict):
                data["channels"] = {}
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            return {"updated_at_unix": 0, "channels": {}}

    def _save_rate_stats(self) -> None:
        try:
            self._rate_stats_path.parent.mkdir(parents=True, exist_ok=True)
            self._rate_stats["updated_at_unix"] = int(time.time())
            self._rate_stats_path.write_text(
                json.dumps(self._rate_stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("failed to persist channel rate-limit stats")

    def _record_rate_limit_event(self, channel: str, event: str, value_ms: int = 0) -> None:
        ch = str(channel or "").strip().lower()
        if not ch:
            return
        channels = self._rate_stats.setdefault("channels", {})
        row = channels.setdefault(
            ch, {"delayed_count": 0, "dropped_count": 0, "last_delay_ms": 0, "last_event_unix": 0}
        )
        now = int(time.time())
        row["last_event_unix"] = now
        if event == "delay":
            row["delayed_count"] = int(row.get("delayed_count", 0)) + 1
            row["last_delay_ms"] = max(0, int(value_ms))
        elif event == "drop":
            row["dropped_count"] = int(row.get("dropped_count", 0)) + 1
        self._save_rate_stats()

    def _resolve_rate_limit(self, channel_name: str) -> tuple[_TokenBucketRateLimiter, str]:
        key = str(channel_name or "").strip().lower()
        limiter = self._channel_rate_limiter.get(key, self._default_rate_limiter)
        mode = self._channel_rate_limit_mode.get(key, self._default_rate_limit_mode)
        return limiter, mode

    @staticmethod
    def _sender_tokens(sender_id: str) -> set[str]:
        sender_str = str(sender_id)
        out = {sender_str}
        if "|" in sender_str:
            for part in sender_str.split("|"):
                token = part.strip()
                if token:
                    out.add(token)
        return out

    def _normalize_id_list(self, values: list[str] | None) -> set[str]:
        out: set[str] = set()
        for v in values or []:
            token = str(v).strip()
            if token:
                out.add(token)
        return out

    def _is_sender_allowed(self, sender_id: str, channel_cfg: Any) -> bool:
        sender_tokens = self._sender_tokens(sender_id)
        global_deny = self._normalize_id_list(getattr(self.config.channels, "deny_from", []))
        if any(tok in global_deny for tok in sender_tokens):
            return False

        global_allow = self._normalize_id_list(getattr(self.config.channels, "allow_from", []))
        if global_allow and not any(tok in global_allow for tok in sender_tokens):
            return False

        admins = self._normalize_id_list(getattr(channel_cfg, "admins", []))
        users = self._normalize_id_list(getattr(channel_cfg, "users", []))
        if admins or users:
            return any(tok in admins or tok in users for tok in sender_tokens)

        allow_list = self._normalize_id_list(getattr(channel_cfg, "allow_from", []))
        if not allow_list:
            return True
        return any(tok in allow_list for tok in sender_tokens)

    def _bind_access_checker(self, channel: BaseChannel) -> None:
        """Inject centralized RBAC checker into channel instance."""
        channel.access_checker = self._is_sender_allowed
        channel.security_policy = self.config.tools.policy

    def _init_channels(self) -> None:
        """Initialize channels based on config."""
        media_root = self.config.workspace_path / "media"
        for spec in iter_channel_specs():
            ch_cfg = get_channel_config(self.config, spec)
            if not bool(getattr(ch_cfg, "enabled", False)):
                continue
            try:
                mod = importlib.import_module(spec.module_path)
                cls = getattr(mod, spec.class_name)
                kwargs: dict[str, Any] = {"media_root": media_root}
                if spec.needs_groq_api_key:
                    kwargs["groq_api_key"] = self.config.providers.groq.api_key
                self.channels[spec.key] = cls(ch_cfg, self.bus, **kwargs)
                self._bind_access_checker(self.channels[spec.key])
                logger.info(f"{spec.display_name} channel enabled")
            except ImportError as e:
                logger.warning(f"{spec.display_name} channel not available: {e}")

        # Expose webhook-backed channels to dashboard API router.
        try:
            from zen_claw.dashboard.webhooks import register_channels

            webhook_channels = {
                spec.webhook_slot: self.channels.get(spec.key)
                for spec in iter_channel_specs()
                if spec.webhook_slot
            }
            register_channels(**webhook_channels)
        except Exception as e:
            logger.debug(f"Webhook route registration skipped: {e}")

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error(f"Failed to start channel {name}: {e}")

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info(f"Starting {name} channel...")
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                channel = self.channels.get(msg.channel)
                trace_id, msg.metadata = TraceContext.ensure_trace_id(msg.metadata)
                if channel:
                    try:
                        self._maybe_attach_routed_agent(msg)
                        limiter_key = f"{msg.channel}:{msg.chat_id}"
                        limiter, mode = self._resolve_rate_limit(msg.channel)
                        if mode == "drop":
                            ok, retry_after = await limiter.try_acquire(limiter_key)
                            if not ok:
                                self._record_rate_limit_event(msg.channel, "drop")
                                logger.warning(
                                    "Outbound message dropped by rate limiter "
                                    + TraceContext.event_text(
                                        "channel.dispatch.rate_dropped",
                                        trace_id,
                                        channel=msg.channel,
                                        chat_id=msg.chat_id,
                                        retry_after_ms=int(retry_after * 1000),
                                    )
                                )
                                await self._maybe_send_drop_notice(channel, msg, trace_id)
                                continue
                            wait_s = 0.0
                        else:
                            wait_s = await limiter.acquire(limiter_key)

                        if wait_s > 0.01:
                            self._record_rate_limit_event(msg.channel, "delay", int(wait_s * 1000))
                            logger.warning(
                                "Outbound dispatch delayed by rate limiter "
                                + TraceContext.event_text(
                                    "channel.dispatch.rate_limited",
                                    trace_id,
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    delay_ms=int(wait_s * 1000),
                                )
                            )
                        logger.info(
                            "Dispatch outbound "
                            + TraceContext.event_text(
                                "channel.dispatch",
                                trace_id,
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                            )
                        )
                        await self._dispatch_message(channel=channel, msg=msg, trace_id=trace_id)
                    except Exception as e:
                        await self._audit_outbound_event(
                            trace_id=trace_id,
                            event_type="message.outbound.failed",
                            msg=msg,
                            error_code="channel_dispatch_runtime",
                            sidecar_target=self._outbound_adapter.sidecar_target,
                        )
                        logger.error(
                            f"Error sending to {msg.channel}: {e} "
                            + TraceContext.event_text(
                                "channel.dispatch.error",
                                trace_id,
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                error_kind="runtime",
                                retryable=True,
                            )
                        )
                else:
                    logger.warning(
                        f"Unknown channel: {msg.channel} "
                        + TraceContext.event_text(
                            "channel.dispatch.unknown",
                            trace_id,
                            channel=msg.channel,
                            error_kind="parameter",
                            retryable=False,
                        )
                    )

            except asyncio.TimeoutError:
                self._maybe_gc_routes()
                continue
            except asyncio.CancelledError:
                break

    async def _maybe_send_drop_notice(
        self,
        channel: BaseChannel,
        msg: OutboundMessage,
        trace_id: str,
    ) -> None:
        if not self._drop_notice_enabled or not self._drop_notice_text:
            return
        key = f"{msg.channel}:{msg.chat_id}"
        now = time.monotonic()
        last = self._last_drop_notice_at.get(key, 0.0)
        if now - last < self._drop_notice_cooldown_sec:
            return
        self._last_drop_notice_at[key] = now
        try:
            await self._dispatch_message(
                channel=channel,
                msg=OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self._drop_notice_text,
                    metadata={"trace_id": trace_id, "rate_limited_notice": True},
                ),
                trace_id=trace_id,
            )
        except Exception:
            await self._audit_outbound_event(
                trace_id=trace_id,
                event_type="message.outbound.failed",
                msg=msg,
                error_code="channel_dispatch_rate_drop_notice_failed",
                sidecar_target=self._outbound_adapter.sidecar_target,
            )
            logger.warning(
                "Failed to send rate-limit drop notice "
                + TraceContext.event_text(
                    "channel.dispatch.rate_drop_notice_failed",
                    trace_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    error_kind="runtime",
                    retryable=True,
                )
            )

    async def _dispatch_message(
        self,
        *,
        channel: BaseChannel,
        msg: OutboundMessage,
        trace_id: str,
    ) -> None:
        mode = self._outbound_adapter.outbound_mode_for_channel(msg.channel)
        if mode == "local_only":
            meta = dict(msg.metadata or {})
            meta["trace_id"] = trace_id
            meta["outbound_dispatch_authorized"] = True
            meta["outbound_dispatch_mode"] = "manager_local_dispatch"
            meta["outbound_dispatch_source"] = "channel_manager"
            msg.metadata = meta
            await channel.send(msg)
            await self._audit_outbound_event(
                trace_id=trace_id,
                event_type="message.outbound.executed",
                msg=msg,
                isolation_mode=mode,
            )
            return

        results = await self._outbound_adapter.dispatch(msg, trace_id=trace_id)
        if not results:
            await self._audit_outbound_event(
                trace_id=trace_id,
                event_type="message.outbound.executed",
                msg=msg,
                isolation_mode=mode,
                sidecar_target=self._outbound_adapter.sidecar_target,
            )
            return
        for action, result in results:
            event_type = "message.outbound.uploaded" if action == "connector.file.upload" and result.ok else "message.outbound.executed" if result.ok else "message.outbound.denied"
            await self._audit_outbound_event(
                trace_id=trace_id,
                event_type=event_type,
                msg=msg,
                action=action,
                result=result,
                isolation_mode=mode,
                sidecar_target=str(result.meta.get("sidecar_target") or self._outbound_adapter.sidecar_target),
            )
            if not result.ok:
                error = result.error.message if result.error else "channel outbound isolation failed"
                raise RuntimeError(error)

    async def _audit_outbound_event(
        self,
        *,
        trace_id: str,
        event_type: str,
        msg: OutboundMessage,
        action: str = "",
        result: Any | None = None,
        error_code: str = "",
        isolation_mode: str = "",
        sidecar_target: str = "",
    ) -> None:
        if self._audit_worker is None:
            return
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        await self._audit_worker.audit_turn(
            trace_id,
            {
                "event_type": event_type,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content_length": len(msg.content or ""),
                "media_count": len(msg.media or []),
                "connector_name": str(msg.channel or "").strip().lower(),
                "capability": str(action or ""),
                "action": str(action or ""),
                "target_resource": (
                    f"channel.{str(msg.channel or '').strip().lower()}.file"
                    if action == "connector.file.upload"
                    else f"channel.{str(msg.channel or '').strip().lower()}.message"
                    if action
                    else ""
                ),
                "identity": {
                    "channel": str(meta.get("origin_channel") or "system"),
                    "sender_id": str(meta.get("sender_id") or meta.get("route_user_id") or ""),
                    "chat_id": str(meta.get("origin_chat_id") or msg.chat_id or ""),
                    "tenant_id": str(meta.get("tenant_id") or "default"),
                    "workspace_id": str(meta.get("workspace_id") or self.config.workspace_path.name),
                    "agent_profile": str(
                        meta.get("agent_profile")
                        or meta.get("profile_id")
                        or meta.get("routed_agent_id")
                        or "default"
                    ),
                    "role": str(meta.get("role") or meta.get("channel_role") or "operator"),
                },
                "policy_snapshot": {
                    "production_hardening": bool(self.config.tools.policy.production_hardening),
                    "legacy_compat": bool(self.config.tools.policy.legacy_compat),
                },
                "dev_profile": bool(meta.get("dev_profile")),
                "legacy_compat": bool(self.config.tools.policy.legacy_compat),
                "isolation_mode": str(isolation_mode or self._outbound_adapter.outbound_mode_for_channel(msg.channel)),
                "sidecar_target": str(sidecar_target or ""),
                "error_code": str(
                    error_code or (getattr(getattr(result, "error", None), "code", "") if result is not None else "")
                ),
            },
        )

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def bind_agent(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        agent_id: str,
        reason: str = "manual_bind",
    ) -> str | None:
        if self._route_store is None:
            return None
        route = self._route_store.set_route(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            agent_id=agent_id,
            reason=reason,
        )
        return route.agent_id

    def resolve_agent(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
    ) -> str | None:
        if self._route_store is None:
            return None
        route = self._route_store.resolve_route(channel=channel, chat_id=chat_id, user_id=user_id)
        return route.agent_id if route else None

    def mark_agent_error(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        current_agent_id: str,
    ) -> str | None:
        if self._route_store is None:
            return None
        rollback = self._route_store.soft_rollback_on_error(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            current_agent_id=current_agent_id,
            grace_period_ms=180_000,
        )
        return rollback.agent_id if rollback else None

    def list_route_audit(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
    ) -> list[dict[str, int | str]]:
        if self._route_store is None:
            return []
        return self._route_store.list_audit(channel=channel, chat_id=chat_id, user_id=user_id)

    def _maybe_attach_routed_agent(self, msg: OutboundMessage) -> None:
        if self._route_store is None:
            return
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        user_id = str(meta.get("route_user_id") or meta.get("sender_id") or "").strip()
        if not user_id:
            return
        agent_id = self.resolve_agent(channel=msg.channel, chat_id=msg.chat_id, user_id=user_id)
        if agent_id and "routed_agent_id" not in meta:
            meta["routed_agent_id"] = agent_id
            msg.metadata = meta

    def _maybe_gc_routes(self) -> None:
        if self._route_store is None:
            return
        now = int(time.time() * 1000)
        if now - self._route_gc_last_ms < 60_000:
            return
        self._route_gc_last_ms = now
        try:
            self._route_store.gc_expired_routes(ttl_ms=self._route_gc_ttl_ms, now_ms=now)
        except Exception:
            return

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {"enabled": True, "running": channel.is_running}
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
