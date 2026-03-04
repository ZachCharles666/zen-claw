"""Approval gate: pause, persist, and résumé sensitive tool calls via user confirmation."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class PendingApproval:
    """A single tool call that is waiting for user confirmation."""

    approval_id: str
    session_id: str
    tool_name: str
    tool_args: dict[str, Any]
    reason: str
    created_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingApproval":
        data = dict(data)
        data["status"] = ApprovalStatus(data.get("status", "pending"))
        return cls(**data)

    def format_request_message(self) -> str:
        """Format the approval request message for sending to the user."""
        args_preview = json.dumps(self.tool_args, ensure_ascii=False, indent=2)
        if len(args_preview) > 500:
            args_preview = args_preview[:500] + "\n... (truncated)"
        return (
            f"⚠️ **[需要授权]** Agent 即将执行敏感操作，请确认:\n\n"
            f"**工具**: `{self.tool_name}`\n"
            f"**原因**: {self.reason}\n"
            f"**参数**:\n```json\n{args_preview}\n```\n\n"
            f"**审批 ID**: `{self.approval_id}`\n\n"
            f"回复 `/approve {self.approval_id}` 授权  |  `/deny {self.approval_id}` 拒绝"
        )


class ApprovalGate:
    """Stateful, file-persisted approval gate for sensitive tool calls.

    When a tool is marked as requiring approval, the gate:
    1. Creates a PendingApproval record and persists it.
    2. Returns the record so the caller can send a notification.
    3. Waits (via ``await_decision``) until the user approves or denies.
    4. On ``approve``/``deny`` calls (from the chat command handler), updates state.
    """

    _SENSITIVE_TOOLS: frozenset[str] = frozenset(
        {
            # Destructive filesystem ops
            "write_file",
            "edit_file",
            # Shell / process control
            "exec",
            "spawn",
            "service_start",
            "service_stop",
            # External network POST (non-read)
            "social_platform_post",
            "social_platform_like",
            # Credential writes
            "credential_store",
        }
    )

    def __init__(
        self,
        data_dir: Path,
        sensitive_tools: frozenset[str] | None = None,
        ttl_seconds: int = 3600,
    ):
        self._store_path = Path(data_dir) / "approvals" / "pending.json"
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._sensitive_tools = sensitive_tools if sensitive_tools is not None else self._SENSITIVE_TOOLS
        self._ttl = ttl_seconds
        self._pending: dict[str, PendingApproval] = {}
        self._load()

    # ── public API ─────────────────────────────────────────────────────────

    def is_sensitive(self, tool_name: str) -> bool:
        """Return True if the tool requires explicit user approval."""
        return (tool_name or "").strip().lower() in self._sensitive_tools

    async def request_approval(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        reason: str = "Sensitive operation",
        bus: Any = None,
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> PendingApproval:
        """Create and persist a new approval request, and optionally notify via bus."""
        approval_id = str(uuid.uuid4())[:8].upper()
        rec = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            reason=reason,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._pending[approval_id] = rec
        self._save()

        if bus:
            try:
                from zen_claw.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=rec.format_request_message()
                ))
            except Exception as exc:
                logger.error(
                    "ApprovalGate: failed to send approval notification "
                    "(approval_id={}, channel={}, chat_id={}): {}",
                    approval_id, channel, chat_id, exc,
                )

        return rec

    def approve(self, approval_id: str) -> PendingApproval | None:
        """Mark an approval request as approved. Returns the record or None if not found."""
        return self._decide(approval_id, ApprovalStatus.APPROVED)

    def deny(self, approval_id: str) -> PendingApproval | None:
        """Mark an approval request as denied. Returns the record or None if not found."""
        return self._decide(approval_id, ApprovalStatus.DENIED)

    def get(self, approval_id: str) -> PendingApproval | None:
        """Look up a pending approval by ID."""
        self._expire_old()
        return self._pending.get(approval_id.strip().upper())

    def list_pending(self, session_id: str | None = None) -> list[PendingApproval]:
        """List all PENDING approvals, optionally filtered by session."""
        self._expire_old()
        rows = [r for r in self._pending.values() if r.status == ApprovalStatus.PENDING]
        if session_id:
            rows = [r for r in rows if r.session_id == session_id]
        return sorted(rows, key=lambda r: r.created_at)

    def consume_approved(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PendingApproval | None:
        """Find and remove the first APPROVED record matching session/tool/args.

        Returns the consumed record, or None if no matching approved record exists.
        Removing the record prevents it from being reused for a different invocation.
        """
        import json as _json

        args_key = _json.dumps(tool_args, sort_keys=True)
        for rec in list(self._pending.values()):
            if (
                rec.status == ApprovalStatus.APPROVED
                and rec.session_id == session_id
                and rec.tool_name == tool_name
                and _json.dumps(rec.tool_args, sort_keys=True) == args_key
            ):
                del self._pending[rec.approval_id]
                self._save()
                return rec
        return None

    # ── internals ──────────────────────────────────────────────────────────

    def _decide(self, approval_id: str, status: ApprovalStatus) -> PendingApproval | None:
        aid = approval_id.strip().upper()
        rec = self._pending.get(aid)
        if not rec or rec.status != ApprovalStatus.PENDING:
            return None
        rec.status = status
        rec.decided_at = datetime.now(UTC).isoformat()
        self._save()
        return rec

    def _expire_old(self) -> None:
        now = datetime.now(UTC)
        changed = False
        for rec in list(self._pending.values()):
            if rec.status != ApprovalStatus.PENDING:
                continue
            try:
                age = (now - datetime.fromisoformat(rec.created_at)).total_seconds()
            except Exception:
                continue
            if age > self._ttl:
                rec.status = ApprovalStatus.EXPIRED
                rec.decided_at = now.isoformat()
                changed = True
        if changed:
            self._save()

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
            for item in raw.get("approvals", []):
                rec = PendingApproval.from_dict(item)
                self._pending[rec.approval_id] = rec
        except Exception:
            pass

    def _save(self) -> None:
        payload = {"approvals": [r.to_dict() for r in self._pending.values()]}
        self._store_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
