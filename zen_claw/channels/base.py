"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.bus.events import InboundMessage, OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.observability.trace import TraceContext


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the zen-claw message bus.
    """

    name: str = "base"
    _MAX_MEDIA_ITEMS: int = 8
    _MAX_MEDIA_PATH_LEN: int = 1024

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
