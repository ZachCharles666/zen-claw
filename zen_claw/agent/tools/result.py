"""Structured tool execution result and error model."""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolErrorKind(str, Enum):
    """Normalized tool error categories."""

    PARAMETER = "parameter"
    PERMISSION = "permission"
    RUNTIME = "runtime"
    RETRYABLE = "retryable"


@dataclass
class ToolError:
    """A normalized tool error payload."""

    kind: ToolErrorKind
    message: str
    code: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "code": self.code,
            "retryable": self.retryable,
        }


@dataclass
class ToolResult:
    """Unified tool result model for both success and error cases."""

    ok: bool
    content: str = ""
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def failure(
        cls,
        kind: ToolErrorKind,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        content: str = "",
        **meta: Any,
    ) -> "ToolResult":
        retryable_flag = (kind == ToolErrorKind.RETRYABLE) if retryable is None else retryable
        return cls(
            ok=False,
            content=content,
            error=ToolError(kind=kind, message=message, code=code, retryable=retryable_flag),
            meta=meta,
        )

    def to_tool_message_content(self) -> str:
        """
        Convert result into message content for LLM tool result role.

        Success keeps raw content for compatibility.
        Error uses a stable JSON payload for machine-readable parsing.
        """
        if self.ok:
            return self.content
        payload = {"ok": False, "error": self.error.to_dict() if self.error else None}
        return "[tool_error] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def to_legacy_text(self) -> str:
        """Convert to legacy plain text format."""
        if self.ok:
            return self.content
        if not self.error:
            return "Error: Unknown tool error"
        return f"Error [{self.error.kind.value}]: {self.error.message}"
