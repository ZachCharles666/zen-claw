"""Message tool for sending messages to users."""

import json
from typing import Any, Awaitable, Callable

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.connector_sidecar import ConnectorSidecarClient
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.events import OutboundMessage
from zen_claw.observability.trace import TraceContext


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        *,
        connector_proxy_url: str = "",
        connector_approval_mode: str = "token",
        connector_approval_token: str = "",
        trusted_local_channels: list[str] | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._trace_id: str | None = None
        self._trusted_local_channels = {
            str(ch).strip().lower()
            for ch in (trusted_local_channels or ["cli", "system"])
            if str(ch).strip()
        }
        self._connector_sidecar = ConnectorSidecarClient(
            proxy_url=connector_proxy_url,
            approval_mode=connector_approval_mode,
            approval_token=connector_approval_token,
        )

    def set_context(self, channel: str, chat_id: str, trace_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._trace_id = trace_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The message content to send"},
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)",
                },
                "chat_id": {"type": "string", "description": "Optional: target chat/user ID"},
            },
            "required": ["content"],
        }

    async def execute(
        self, content: str, channel: str | None = None, chat_id: str | None = None, **kwargs: Any
    ) -> ToolResult:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id

        if not channel or not chat_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "No target channel/chat specified",
                code="message_target_missing",
            )

        trace_id = str(kwargs.get("trace_id") or self._trace_id or "").strip()
        target_channel = str(channel or "").strip().lower()
        security_context = dict(kwargs.get("security_context") or {})
        if target_channel and target_channel not in self._trusted_local_channels:
            if not self._connector_sidecar.proxy_url:
                return ToolResult.failure(
                    ToolErrorKind.RUNTIME,
                    "External message sending requires connector sidecar configuration",
                    code="message_send_sidecar_not_configured",
                )
            request_payload = self._connector_sidecar.build_request_payload(
                target_url=f"channel://{target_channel}/{chat_id}",
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "channel": channel,
                        "chat_id": chat_id,
                        "content": content,
                        "reply_to": kwargs.get("reply_to"),
                        "media": list(kwargs.get("media") or []),
                        "metadata": dict(kwargs.get("metadata") or {}),
                    },
                    ensure_ascii=False,
                ),
                connector_name=target_channel,
                action="connector.message.send",
                target_resource=f"channel.{target_channel}.message",
                security_context={
                    **security_context,
                    "channel": str(security_context.get("channel") or target_channel).strip().lower(),
                    "chat_id": str(security_context.get("chat_id") or chat_id).strip(),
                },
            )
            return await self._connector_sidecar.execute_request(
                request_payload=request_payload,
                trace_id=trace_id,
            )

        if not self._send_callback:
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                "Message sending not configured",
                code="message_send_not_configured",
            )

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=TraceContext.child_metadata(self._trace_id or kwargs.get("trace_id")),
        )

        try:
            await self._send_callback(msg)
            return ToolResult.success(f"Message sent to {channel}:{chat_id}")
        except Exception as e:
            return ToolResult.failure(
                ToolErrorKind.RETRYABLE,
                f"Error sending message: {str(e)}",
                code="message_send_failed",
            )
