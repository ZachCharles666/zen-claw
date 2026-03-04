"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.events import OutboundMessage
from zen_claw.observability.trace import TraceContext


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = ""
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._trace_id: str | None = None

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
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any
    ) -> ToolResult:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id

        if not channel or not chat_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "No target channel/chat specified",
                code="message_target_missing",
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


