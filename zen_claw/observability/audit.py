"""Audit worker — writes structured JSONL audit records for all agent actions."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


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
    ):
        self.config = config or {}
        self.cpu_limit = self.config.get("cpu_limit", "0.5")
        self.mem_limit = self.config.get("mem_limit", "512Mi")
        self.readonly_fs = self.config.get("readonly_fs", True)
        self._data_dir = Path(data_dir) if data_dir else None
        self._write_lock = asyncio.Lock()

        logger.info(
            f"AuditWorker initialized: data_dir={self._data_dir}, "
            f"CPU={self.cpu_limit}, MEM={self.mem_limit}, ReadOnlyFS={self.readonly_fs}"
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
            True on success or if no data_dir is configured.
        """
        path = self._audit_log_path()
        if path is None:
            return True

        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "trace_id": trace_id,
            **turn_data,
        }

        try:
            async with self._write_lock:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._append_record, path, record)
        except Exception as exc:
            logger.warning(f"audit_turn write failed (non-fatal): {exc}")
            return False

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _audit_log_path(self) -> Path | None:
        if self._data_dir is None:
            return None
        day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        log_dir = self._data_dir / "audit_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"{day}.jsonl"

    def _append_record(self, path: Path, record: dict) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


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
