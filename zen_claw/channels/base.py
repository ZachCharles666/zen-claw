"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from loguru import logger

from zen_claw.bus.events import InboundMessage, OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.config.loader import get_data_dir
from zen_claw.observability.audit import AuditWorker
from zen_claw.observability.trace import TraceContext
from zen_claw.security_context import (
    is_trusted_local_surface,
    issue_gateway_envelope,
    security_policy_snapshot,
)


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the zen-claw message bus.
    """

    name: str = "base"
    _MAX_MEDIA_ITEMS: int = 8
    _MAX_MEDIA_PATH_LEN: int = 1024
    _LOCAL_ONLY_CHANNELS = frozenset({"cli", "system"})
    _OUTBOUND_AUTHORIZED_KEY = "outbound_dispatch_authorized"
    _OUTBOUND_MODE_KEY = "outbound_dispatch_mode"
    _OUTBOUND_SOURCE_KEY = "outbound_dispatch_source"

    def __init__(self, config: Any, bus: MessageBus, media_root: Path | None = None):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self._running = False
        self.media_root = media_root or (Path.home() / ".zen-claw" / "media")
        self.access_checker = None  # Optional manager-injected access hook.
        self.security_policy: Any | None = None
        self._outbound_guard_state: ContextVar[dict[str, Any] | None] = ContextVar(
            f"channel_outbound_guard_{self.name}",
            default=None,
        )

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.

        Args:
            sender_id: The sender's identifier.

        Returns:
            True if allowed, False otherwise.
        """
        if callable(self.access_checker):
            return bool(self.access_checker(str(sender_id), self.config))

        sender_tokens = self._sender_tokens(sender_id)
        admins = {str(v).strip() for v in getattr(self.config, "admins", []) if str(v).strip()}
        users = {str(v).strip() for v in getattr(self.config, "users", []) if str(v).strip()}

        # RBAC mode: if admins/users is configured, only listed identities are allowed.
        if admins or users:
            return any(tok in admins or tok in users for tok in sender_tokens)

        allow_list = {
            str(v).strip() for v in getattr(self.config, "allow_from", []) if str(v).strip()
        }
        if not allow_list:
            logger.warning(
                "Channel {}: no admins/users/allow_from configured — "
                "denying access for sender '{}'. "
                "Set admins or allow_from in the channel config to grant access.",
                self.name,
                sender_id,
            )
            return False
        return any(tok in allow_list for tok in sender_tokens)

    def get_role(self, sender_id: str) -> str:
        """Resolve sender role for RBAC policy: admin|user|guest."""
        sender_tokens = self._sender_tokens(sender_id)
        admins = {str(v).strip() for v in getattr(self.config, "admins", []) if str(v).strip()}
        users = {str(v).strip() for v in getattr(self.config, "users", []) if str(v).strip()}
        if any(tok in admins for tok in sender_tokens):
            return "admin"
        if any(tok in users for tok in sender_tokens):
            return "user"
        return "guest"

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
        """
        trace_id, normalized_meta = TraceContext.ensure_trace_id(metadata)
        role = self.get_role(sender_id)
        normalized_meta["channel_role"] = role
        normalized_meta["identity_verified"] = True
        trusted_locals = {
            str(ch).strip().lower()
            for ch in getattr(self._effective_security_policy(), "trusted_local_channels", []) or []
            if str(ch).strip()
        }
        security_context = issue_gateway_envelope(
            trace_id=trace_id,
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            tenant_id=str(normalized_meta.get("tenant_id") or normalized_meta.get("tid") or "default"),
            workspace_id=str(normalized_meta.get("workspace_id") or ""),
            agent_profile=str(
                normalized_meta.get("agent_profile")
                or getattr(self.config, "agent_profile", "")
                or "default"
            ),
            role=str(normalized_meta.get("role") or role or "guest"),
            trust_level=(
                "trusted_local"
                if is_trusted_local_surface(self.name, trusted_locals | set(self._LOCAL_ONLY_CHANNELS))
                else "remote_untrusted"
            ),
            origin_surface=str(normalized_meta.get("origin_surface") or self.name),
            channel_role=role,
            policy_snapshot=security_policy_snapshot(
                production_hardening=bool(
                    getattr(self._effective_security_policy(), "production_hardening", True)
                ),
                legacy_compat=bool(getattr(self._effective_security_policy(), "legacy_compat", False)),
                restrict_to_workspace=False,
            ),
        )
        normalized_meta.update(security_context)

        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allowFrom/admins/users list in config to grant access. "
                f"{TraceContext.event_text('channel.access.denied', trace_id, channel=self.name, error_kind='permission', retryable=False)}"
            )
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=self._sanitize_media(
                self._merge_media_with_metadata_refs(media, normalized_meta)
            ),
            metadata=normalized_meta,
        )

        await self.bus.publish_inbound(msg)

    def _sender_tokens(self, sender_id: str) -> set[str]:
        sender_str = str(sender_id)
        out = {sender_str}
        if "|" in sender_str:
            for part in sender_str.split("|"):
                token = part.strip()
                if token:
                    out.add(token)
        return out

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running

    def _sanitize_media(self, media: list[str] | None) -> list[str]:
        """Normalize and bound media list from channels."""
        if not media:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in media:
            if len(out) >= self._MAX_MEDIA_ITEMS:
                break
            if not isinstance(raw, str):
                continue
            item = raw.strip()
            if not item:
                continue
            if len(item) > self._MAX_MEDIA_PATH_LEN:
                continue
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def _merge_media_with_metadata_refs(
        self,
        media: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> list[str]:
        """Merge channel media list with optional metadata.media_refs for consistency."""
        merged: list[str] = list(media or [])
        refs = (metadata or {}).get("media_refs")
        if isinstance(refs, list):
            for item in refs:
                if isinstance(item, str):
                    merged.append(item)
        return merged

    def _build_media_uri(self, source: str, media_type: str, media_id: str) -> str:
        """Build canonical cross-channel media URI."""
        src = str(source or "unknown").strip().lower()
        mtype = str(media_type or "file").strip().lower()
        mid = str(media_id or "").strip()
        return f"media://{src}/{mtype}/{mid}"

    def _effective_security_policy(self) -> Any:
        policy = getattr(self, "security_policy", None)
        if policy is not None:
            return policy
        return SimpleNamespace(
            production_hardening=True,
            legacy_compat=False,
            trusted_local_channels=["cli", "system"],
        )

    def _is_local_only_channel(self) -> bool:
        policy = self._effective_security_policy()
        trusted = {
            str(ch).strip().lower()
            for ch in getattr(policy, "trusted_local_channels", []) or []
            if str(ch).strip()
        }
        return is_trusted_local_surface(self.name, trusted | set(self._LOCAL_ONLY_CHANNELS))

    @contextmanager
    def _allow_outbound_helpers(
        self,
        *,
        trace_id: str,
        dispatch_mode: str,
        send_path: str,
    ):
        token = self._outbound_guard_state.set(
            {
                "trace_id": trace_id,
                "dispatch_mode": str(dispatch_mode or ""),
                "send_path": str(send_path or "send"),
            }
        )
        try:
            yield
        finally:
            self._outbound_guard_state.reset(token)

    async def _authorize_outbound_send(
        self,
        msg: OutboundMessage,
        *,
        send_path: str = "send",
    ) -> tuple[str, str]:
        trace_id, meta = TraceContext.ensure_trace_id(msg.metadata)
        msg.metadata = meta
        if self._is_local_only_channel():
            return trace_id, "local_only"

        dispatch_mode = str(meta.get(self._OUTBOUND_MODE_KEY) or "").strip()
        if bool(meta.get(self._OUTBOUND_AUTHORIZED_KEY)) and dispatch_mode:
            return trace_id, dispatch_mode

        await self._audit_outbound_guard_event(
            trace_id=trace_id,
            event_type="message.outbound.direct_blocked",
            path_name=str(send_path or "send"),
            msg=msg,
            isolation_mode="guardrail_blocked",
            error_code="external_channel_direct_send_blocked",
        )
        raise RuntimeError("external outbound requires authorized dispatch context")

    async def _assert_outbound_helper_allowed(
        self,
        *,
        helper_name: str,
        trace_id: str | None = None,
        msg: OutboundMessage | None = None,
    ) -> tuple[str, str]:
        state = self._outbound_guard_state.get()
        if state:
            return str(state.get("trace_id") or trace_id or ""), str(
                state.get("dispatch_mode") or ""
            )

        policy = self._effective_security_policy()
        helper_trace = str(trace_id or (msg.trace_id if msg else "") or TraceContext.new_trace_id())
        del policy

        await self._audit_outbound_guard_event(
            trace_id=helper_trace,
            event_type="message.outbound.helper_blocked",
            path_name=helper_name,
            msg=msg,
            isolation_mode="guardrail_blocked",
            error_code="external_channel_helper_blocked",
        )
        raise RuntimeError("external outbound helper requires authorized dispatch context")

    async def _audit_outbound_guard_event(
        self,
        *,
        trace_id: str,
        event_type: str,
        path_name: str,
        msg: OutboundMessage | None,
        isolation_mode: str,
        error_code: str,
    ) -> None:
        try:
            audit = AuditWorker(data_dir=get_data_dir())
            meta = dict(msg.metadata or {}) if msg is not None else {}
            await audit.audit_turn(
                trace_id,
                {
                    "event_type": event_type,
                    "channel": self.name,
                    "chat_id": str((msg.chat_id if msg is not None else meta.get("chat_id")) or ""),
                    "content_length": len(str(msg.content or "")) if msg is not None else 0,
                    "media_count": len(msg.media or []) if msg is not None else 0,
                    "connector_name": self.name,
                    "action": "connector.file.upload"
                    if "helper" in event_type and "upload" in path_name
                    else "connector.message.send",
                    "target_resource": (
                        f"channel.{self.name}.file"
                        if "upload" in path_name
                        else f"channel.{self.name}.message"
                    ),
                    "identity": {
                        "channel": str(meta.get("origin_channel") or self.name or "system"),
                        "sender_id": str(meta.get("sender_id") or meta.get("route_user_id") or ""),
                        "chat_id": str(meta.get("origin_chat_id") or meta.get("chat_id") or ""),
                        "tenant_id": str(meta.get("tenant_id") or "default"),
                        "workspace_id": str(meta.get("workspace_id") or ""),
                        "agent_profile": str(
                            meta.get("agent_profile")
                            or meta.get("profile_id")
                            or meta.get("routed_agent_id")
                            or "default"
                        ),
                        "role": str(meta.get("role") or meta.get("channel_role") or "operator"),
                    },
                    "policy_snapshot": {
                        "production_hardening": bool(
                            getattr(self._effective_security_policy(), "production_hardening", True)
                        ),
                        "legacy_compat": bool(
                            getattr(self._effective_security_policy(), "legacy_compat", False)
                        ),
                    },
                    "legacy_compat": bool(
                        getattr(self._effective_security_policy(), "legacy_compat", False)
                    ),
                    "dev_profile": bool(meta.get("dev_profile")),
                    "isolation_mode": isolation_mode,
                    "send_path": str(path_name or ""),
                    "helper_name": str(path_name or ""),
                    "sidecar_target": "",
                    "error_code": error_code,
                },
            )
        except Exception:
            return
