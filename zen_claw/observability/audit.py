"""Audit worker — writes structured JSONL audit records for all agent actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from zen_claw.utils.crypto import sign_data, verify_signature

# ---------------------------------------------------------------------------
# Audit Sink Protocol + Implementations
# ---------------------------------------------------------------------------

@runtime_checkable
class AuditSink(Protocol):
    """Protocol for audit record destinations."""

    async def emit(self, record: dict[str, Any]) -> bool:
        """Emit one audit record. Returns True on success."""
        ...  # pragma: no cover


class LocalFileSink:
    """Write audit records to local JSONL files (the default sink)."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._write_lock = asyncio.Lock()

    async def emit(self, record: dict[str, Any]) -> bool:
        path = self._audit_log_path()
        try:
            async with self._write_lock:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._append, path, record)
            return True
        except Exception as exc:
            logger.warning(f"LocalFileSink write failed: {exc}")
            return False

    def _audit_log_path(self) -> Path:
        day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        log_dir = self._data_dir / "audit_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"{day}.jsonl"

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class HttpSink:
    """Forward audit records to an HTTP endpoint (e.g. SIEM / log aggregator)."""

    def __init__(self, endpoint: str, auth_token: str = "", batch_size: int = 1):
        self._endpoint = endpoint
        self._auth_token = auth_token
        self._batch_size = max(1, batch_size)
        self._buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def emit(self, record: dict[str, Any]) -> bool:
        async with self._lock:
            self._buffer.append(record)
            if len(self._buffer) < self._batch_size:
                return True
            batch = list(self._buffer)
            self._buffer.clear()
        return await self._send(batch)

    async def flush(self) -> bool:
        async with self._lock:
            if not self._buffer:
                return True
            batch = list(self._buffer)
            self._buffer.clear()
        return await self._send(batch)

    async def _send(self, batch: list[dict[str, Any]]) -> bool:
        try:
            import httpx

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._auth_token:
                headers["Authorization"] = f"Bearer {self._auth_token}"
            body = json.dumps(batch, ensure_ascii=False)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._endpoint, headers=headers, content=body)
                if resp.status_code >= 400:
                    logger.warning(f"HttpSink POST failed: HTTP {resp.status_code}")
                    return False
            return True
        except Exception as exc:
            logger.warning(f"HttpSink error: {exc}")
            return False


class SyslogSink:
    """Forward audit records to syslog."""

    def __init__(self, address: str):
        self._address = address
        self._handler = None

    async def emit(self, record: dict[str, Any]) -> bool:
        try:
            if self._handler is None:
                import logging.handlers

                host, _, port_str = self._address.rpartition(":")
                port = int(port_str) if port_str.isdigit() else 514
                host = host or "localhost"
                self._handler = logging.handlers.SysLogHandler(address=(host, port))
            line = json.dumps(record, ensure_ascii=False)
            import logging

            log_record = logging.LogRecord(
                name="zen_claw.audit", level=logging.INFO, pathname="", lineno=0,
                msg=line, args=(), exc_info=None,
            )
            self._handler.emit(log_record)
            return True
        except Exception as exc:
            logger.warning(f"SyslogSink error: {exc}")
            return False


class AuditWorker:
    """
    Worker responsible for writing audit records to JSONL files.

    Records are written to <data_dir>/audit_logs/YYYY-MM-DD.jsonl (UTC day).
    Each record is a single JSON line with: ts, trace_id, event_type, and
    event-specific payload fields. Sensitive content (message bodies, tool
    parameters) is intentionally excluded to prevent credential leakage.

    Security:
    - Egress restricted to LLM Proxy via network policies (implied by runner).
    - Resource limits (CPU/Memory) enforced via runtime config.
    - File system access restricted to read-only for skill directories.
    - Seccomp profile used to restrict syscalls.
    """

    def __init__(
        self,
        data_dir: Path | str | None = None,
        config: dict | None = None,
        sinks: list[AuditSink] | None = None,
    ):
        self.config = config or {}
        self.cpu_limit = self.config.get("cpu_limit", "0.5")
        self.mem_limit = self.config.get("mem_limit", "512Mi")
        self.readonly_fs = self.config.get("readonly_fs", True)
        self._data_dir = Path(data_dir) if data_dir else None
        self._write_lock = asyncio.Lock()

        # Build sink chain: local file sink (always) + optional external sinks.
        self._sinks: list[AuditSink] = []
        if self._data_dir is not None:
            self._sinks.append(LocalFileSink(self._data_dir))
        if sinks:
            self._sinks.extend(sinks)

        logger.info(
            f"AuditWorker initialized: data_dir={self._data_dir}, "
            f"sinks={len(self._sinks)}, CPU={self.cpu_limit}, MEM={self.mem_limit}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def audit_turn(self, trace_id: str, turn_data: dict) -> bool:
        """
        Write one audit record.

        Args:
            trace_id: Trace identifier for correlation.
            turn_data: Dict with at minimum an ``event_type`` key plus any
                       event-specific fields. Must NOT contain raw message
                       content or tool parameters.

        Returns:
            True on success or if no sinks are configured.
        """
        if not self._sinks:
            return True

        prev_hash = self._get_prev_hash()
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "trace_id": trace_id,
            "prev_hash": prev_hash,
            **turn_data,
        }
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record["hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        record["signature"] = sign_data(canonical)

        # Broadcast to all sinks — failure of external sinks is non-fatal.
        results = await asyncio.gather(
            *(sink.emit(record) for sink in self._sinks),
            return_exceptions=True,
        )
        all_ok = all(r is True for r in results)
        if not all_ok:
            failed = [
                type(s).__name__ for s, r in zip(self._sinks, results) if r is not True
            ]
            logger.warning(f"audit_turn: failed sinks: {failed}")
        return all_ok

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_prev_hash(self) -> str:
        """Read the latest record hash from the local JSONL file for chain continuity."""
        if self._data_dir is None:
            return ""
        day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        path = self._data_dir / "audit_logs" / f"{day}.jsonl"
        if not path.exists():
            return ""
        try:
            with open(path, "rb") as fh:
                lines = fh.read().splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                row = json.loads(line.decode("utf-8"))
                if isinstance(row, dict):
                    return str(row.get("hash") or "")
        except Exception:
            return ""
        return ""


def verify_audit_log(path: Path) -> dict[str, object]:
    """Verify audit hash chain and signatures for one JSONL log."""
    if not path.exists():
        return {"ok": True, "checked": 0, "error": "", "error_index": None}
    prev_hash = ""
    checked = 0
    with open(path, "r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                return {"ok": False, "checked": checked, "error": "invalid_record", "error_index": idx}
            signature = str(row.get("signature") or "")
            observed_hash = str(row.get("hash") or "")
            base = dict(row)
            base.pop("signature", None)
            base.pop("hash", None)
            if str(base.get("prev_hash") or "") != prev_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "error": "audit hash chain broken",
                    "error_index": idx,
                }
            canonical = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if observed_hash != expected_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "error": "audit hash mismatch",
                    "error_index": idx,
                }
            if signature and not verify_signature(canonical, signature):
                return {
                    "ok": False,
                    "checked": checked,
                    "error": "audit signature mismatch",
                    "error_index": idx,
                }
            prev_hash = observed_hash
            checked += 1
    return {"ok": True, "checked": checked, "error": "", "error_index": None}


def verify_security_replay_consistency(path: Path) -> dict[str, object]:
    """Verify same-trace request/policy consistency across approval and execution events."""
    if not path.exists():
        return {"ok": True, "checked": 0, "mismatches": [], "error": ""}

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    checked = 0
    with open(path, "r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            approval = row.get("approval")
            approval = approval if isinstance(approval, dict) else {}
            gateway_meta = row.get("gateway_meta")
            gateway_meta = gateway_meta if isinstance(gateway_meta, dict) else {}
            request_hash = str(
                row.get("request_hash")
                or approval.get("request_hash")
                or gateway_meta.get("request_hash")
                or ""
            ).strip()
            if not request_hash:
                continue
            trace_id = str(row.get("trace_id") or "").strip()
            key = (trace_id, request_hash)
            group = groups.setdefault(
                key,
                {
                    "policy_snapshot_hashes": set(),
                    "gateway_instances": set(),
                    "event_types": [],
                    "line_indexes": [],
                    "decision_ids": set(),
                },
            )
            policy_snapshot_hash = str(
                row.get("policy_snapshot_hash")
                or approval.get("policy_snapshot_hash")
                or gateway_meta.get("policy_snapshot_hash")
                or ""
            ).strip()
            if policy_snapshot_hash:
                group["policy_snapshot_hashes"].add(policy_snapshot_hash)
            gateway_instance = str(
                row.get("gateway_instance") or gateway_meta.get("gateway_instance") or ""
            ).strip()
            if gateway_instance:
                group["gateway_instances"].add(gateway_instance)
            event_type = str(row.get("event_type") or "").strip()
            if event_type:
                group["event_types"].append(event_type)
            decision_id = str(row.get("decision_id") or approval.get("approval_id") or "").strip()
            if decision_id:
                group["decision_ids"].add(decision_id)
            group["line_indexes"].append(idx)
            checked += 1

    mismatches: list[dict[str, Any]] = []
    for (trace_id, request_hash), group in groups.items():
        policy_hashes = set(group["policy_snapshot_hashes"])
        gateway_instances = set(group["gateway_instances"])
        event_types = list(group["event_types"])
        if len(policy_hashes) > 1:
            mismatches.append(
                {
                    "trace_id": trace_id,
                    "request_hash": request_hash,
                    "error": "policy_snapshot_hash_mismatch",
                    "event_types": event_types,
                    "line_indexes": list(group["line_indexes"]),
                }
            )
            continue
        if len(gateway_instances) > 1:
            mismatches.append(
                {
                    "trace_id": trace_id,
                    "request_hash": request_hash,
                    "error": "gateway_instance_mismatch",
                    "event_types": event_types,
                    "line_indexes": list(group["line_indexes"]),
                }
            )
            continue
        has_approval_requested = "approval.requested" in event_types
        has_approval_decision = any(
            event in {"approval.approved", "approval.denied", "approval.expired"} for event in event_types
        )
        has_execution = any(
            event.startswith("tool.")
            or event.startswith("message.outbound.")
            or event in {"approval.execution_started", "approval.execution_finished"}
            for event in event_types
        )
        if has_approval_decision and not has_approval_requested:
            mismatches.append(
                {
                    "trace_id": trace_id,
                    "request_hash": request_hash,
                    "error": "approval_decision_without_request",
                    "event_types": event_types,
                    "line_indexes": list(group["line_indexes"]),
                }
            )
            continue
        if "approval.execution_started" in event_types and not (
            has_approval_requested or "approval.approved" in event_types
        ):
            mismatches.append(
                {
                    "trace_id": trace_id,
                    "request_hash": request_hash,
                    "error": "execution_started_without_approval_chain",
                    "event_types": event_types,
                    "line_indexes": list(group["line_indexes"]),
                }
            )
            continue
        if (has_approval_decision or "approval.execution_started" in event_types) and not has_execution:
            mismatches.append(
                {
                    "trace_id": trace_id,
                    "request_hash": request_hash,
                    "error": "approval_chain_missing_execution",
                    "event_types": event_types,
                    "line_indexes": list(group["line_indexes"]),
                }
            )
    return {
        "ok": len(mismatches) == 0,
        "checked": checked,
        "groups": len(groups),
        "mismatches": mismatches,
        "error": "" if not mismatches else str(mismatches[0].get("error") or "replay_mismatch"),
    }


def get_isolation_config() -> dict:
    """Return system-level isolation parameters for the audit worker."""
    return {
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "securityContext": {
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
