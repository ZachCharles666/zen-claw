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

from zen_claw.observability.audit import AuditWorker
from zen_claw.security_context import stable_json_hash


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
    trace_id: str = ""
    capability: str = ""
    resource_scope: dict[str, Any] | None = None
    security_context: dict[str, Any] | None = None
    policy_snapshot_hash: str = ""
    request_hash: str = ""
    sidecar_target: str = ""
    actor: str = ""
    decision_basis: str = ""
    ttl_seconds: int = 0
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
        identity = dict(self.security_context or {})
        resource_scope = dict(self.resource_scope or {})
        return (
            f"⚠️ **[需要授权]** Agent 即将执行敏感操作，请确认:\n\n"
            f"**工具**: `{self.tool_name}`\n"
            f"**能力**: `{self.capability or self.tool_name}`\n"
            f"**原因**: {self.reason}\n"
            f"**身份**: `{identity.get('channel') or '-'} / {identity.get('sender_id') or '-'} / {identity.get('chat_id') or '-'} / {identity.get('tenant_id') or '-'} / {identity.get('workspace_id') or '-'}`\n"
            f"**来源**: `{identity.get('origin_surface') or '-'} / {identity.get('trust_level') or '-'}`\n"
            f"**资源范围**:\n```json\n{json.dumps(resource_scope, ensure_ascii=False, indent=2)}\n```\n"
            f"**策略快照**: `{self.policy_snapshot_hash or '-'}`\n"
            f"**Sidecar**: `{self.sidecar_target or '-'}`\n"
            f"**TTL**: `{self.ttl_seconds or 0}s`\n"
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
        self._audit = AuditWorker(data_dir=data_dir)
        self._sensitive_tools = (
            sensitive_tools if sensitive_tools is not None else self._SENSITIVE_TOOLS
        )
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
        trace_id: str = "",
        capability: str = "",
        resource_scope: dict[str, Any] | None = None,
        security_context: dict[str, Any] | None = None,
        policy_snapshot_hash: str = "",
        sidecar_target: str = "",
        actor: str = "",
        decision_basis: str = "",
    ) -> PendingApproval:
        """Create and persist a new approval request, and optionally notify via bus."""
        approval_scope = dict(resource_scope or {})
        approval_security_context = dict(security_context or {})
        request_hash = stable_json_hash(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
                "capability": capability or tool_name,
                "resource_scope": approval_scope,
                "security_context": approval_security_context,
                "policy_snapshot_hash": policy_snapshot_hash,
                "sidecar_target": sidecar_target,
            }
        )
        approval_id = str(uuid.uuid4())[:8].upper()
        rec = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            reason=reason,
            created_at=datetime.now(UTC).isoformat(),
            trace_id=str(trace_id or "").strip(),
            capability=str(capability or tool_name).strip().lower(),
            resource_scope=approval_scope,
            security_context=approval_security_context,
            policy_snapshot_hash=str(policy_snapshot_hash or "").strip(),
            request_hash=request_hash,
            sidecar_target=str(sidecar_target or "").strip(),
            actor=str(actor or "").strip(),
            decision_basis=str(decision_basis or reason or "").strip(),
            ttl_seconds=int(self._ttl),
        )
        self._pending[approval_id] = rec
        self._save()
        await self._audit_event(
            rec.trace_id,
            "approval.requested",
            rec,
            extra={"channel": channel, "chat_id": chat_id},
        )

        if bus:
            try:
                from zen_claw.bus.events import OutboundMessage

                await bus.publish_outbound(
                    OutboundMessage(
                        channel=channel, chat_id=chat_id, content=rec.format_request_message()
                    )
                )
            except Exception as exc:
                logger.error(
                    "ApprovalGate: failed to send approval notification "
                    "(approval_id={}, channel={}, chat_id={}): {}",
                    approval_id,
                    channel,
                    chat_id,
                    exc,
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
                self._sync_audit_event(
                    rec.trace_id,
                    "approval.execution_started",
                    rec,
                )
                return rec
        return None

    def record_execution_finished(
        self,
        rec: PendingApproval,
        *,
        ok: bool,
        error_code: str = "",
    ) -> None:
        """Append execution finished audit event for a consumed approval."""
        self._sync_audit_event(
            rec.trace_id,
            "approval.execution_finished",
            rec,
            extra={"ok": bool(ok), "error_code": str(error_code or "")},
        )

    # ── internals ──────────────────────────────────────────────────────────

    def _decide(self, approval_id: str, status: ApprovalStatus) -> PendingApproval | None:
        aid = approval_id.strip().upper()
        rec = self._pending.get(aid)
        if not rec or rec.status != ApprovalStatus.PENDING:
            return None
        rec.status = status
        rec.decided_at = datetime.now(UTC).isoformat()
        self._save()
        self._sync_audit_event(
            rec.trace_id,
            f"approval.{status.value}",
            rec,
        )
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
                self._sync_audit_event(
                    rec.trace_id,
                    "approval.expired",
                    rec,
                )
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

    async def _audit_event(
        self,
        trace_id: str,
        event_type: str,
        rec: PendingApproval,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self._audit.audit_turn(
            trace_id or rec.trace_id or rec.approval_id,
            {
                "event_type": event_type,
                "approval": {
                    "approval_id": rec.approval_id,
                    "session_id": rec.session_id,
                    "tool_name": rec.tool_name,
                    "capability": rec.capability,
                    "status": rec.status.value,
                    "request_hash": rec.request_hash,
                    "policy_snapshot_hash": rec.policy_snapshot_hash,
                    "sidecar_target": rec.sidecar_target,
                    "actor": rec.actor,
                    "decision_basis": rec.decision_basis,
                    "ttl_seconds": rec.ttl_seconds,
                },
                "identity": dict(rec.security_context or {}),
                "resource_scope": dict(rec.resource_scope or {}),
                **dict(extra or {}),
            },
        )

    def _sync_audit_event(
        self,
        trace_id: str,
        event_type: str,
        rec: PendingApproval,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            import asyncio

            asyncio.run(self._audit_event(trace_id, event_type, rec, extra=extra))
            return
        loop.create_task(self._audit_event(trace_id, event_type, rec, extra=extra))
