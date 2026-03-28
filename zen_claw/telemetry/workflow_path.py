"""Workflow path builder — B2.

Reconstructs the decision path for a single agent turn from existing JSONL
event logs.  Steps are assembled into a chronological timeline that the
Dashboard can render as an HTML timeline.

Log files read (all optional — missing files produce empty step lists):
  dashboard/intent_router.log.jsonl
  dashboard/model_routing.log.jsonl
  dashboard/workflow_webhook.log.jsonl
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Step dataclass ────────────────────────────────────────────────────────────

@dataclass
class WorkflowStep:
    """One step in the agent decision timeline."""

    kind: str           # "intent_router" | "model_routing" | "skill_call" | "webhook" | ...
    at_ms: int
    title: str
    status: str = ""    # e.g. "direct_success", "llm_fallback", "ok", "error"
    detail: str = ""
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at_ms": self.at_ms,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "trace_id": self.trace_id,
        }


@dataclass
class WorkflowPath:
    """Ordered timeline of steps for one agent turn / session."""

    trace_id: str = ""
    session_id: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "step_count": len(self.steps),
            "steps": [s.to_dict() for s in self.steps],
        }


# ── Builder ───────────────────────────────────────────────────────────────────

class WorkflowPathBuilder:
    """Build :class:`WorkflowPath` objects from dashboard event logs.

    Parameters
    ----------
    data_dir:
        Project data directory (the directory that contains the ``dashboard/``
        sub-folder where the JSONL logs live).
    """

    _LOG_FILES = {
        "intent_router": "dashboard/intent_router.log.jsonl",
        "model_routing": "dashboard/model_routing.log.jsonl",
        "workflow_webhook": "dashboard/workflow_webhook.log.jsonl",
    }

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)

    # ── public ────────────────────────────────────────────────────────────

    def build(self, *, trace_id: str = "", session_id: str = "") -> WorkflowPath:
        """Return the timeline for a given *trace_id* or *session_id*.

        If neither is provided, returns the most recent event's path.
        """
        if not trace_id and not session_id:
            return WorkflowPath()

        all_steps: list[WorkflowStep] = []
        for kind, rel_path in self._LOG_FILES.items():
            rows = self._read_jsonl(self._data_dir / rel_path)
            for row in rows:
                row_trace = str(row.get("trace_id") or "")
                row_session = str(row.get("session_id") or "")
                if trace_id and row_trace != trace_id:
                    continue
                if session_id and not trace_id and row_session != session_id:
                    continue
                step = self._row_to_step(kind, row)
                if step is not None:
                    all_steps.append(step)

        all_steps.sort(key=lambda s: s.at_ms)
        return WorkflowPath(
            trace_id=trace_id,
            session_id=session_id,
            steps=all_steps,
        )

    def recent(self, *, limit: int = 20) -> list[WorkflowPath]:
        """Return up to *limit* distinct recent trace_ids with their steps."""
        all_rows: list[dict[str, Any]] = []
        for kind, rel_path in self._LOG_FILES.items():
            for row in self._read_jsonl(self._data_dir / rel_path):
                row["_kind"] = kind
                all_rows.append(row)

        # Collect unique trace_ids in reverse chronological order
        seen_traces: dict[str, list[dict[str, Any]]] = {}
        for row in sorted(all_rows, key=lambda r: int(r.get("at_ms") or 0), reverse=True):
            tid = str(row.get("trace_id") or "")
            if not tid:
                continue
            seen_traces.setdefault(tid, []).append(row)
            if len(seen_traces) >= limit:
                break

        paths: list[WorkflowPath] = []
        for tid, rows in list(seen_traces.items())[:limit]:
            steps = []
            for row in rows:
                step = self._row_to_step(str(row.pop("_kind", "")), row)
                if step:
                    steps.append(step)
            steps.sort(key=lambda s: s.at_ms)
            paths.append(WorkflowPath(trace_id=tid, steps=steps))
        return paths

    # ── private ───────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_step(kind: str, row: dict[str, Any]) -> WorkflowStep | None:
        at_ms = int(row.get("at_ms") or 0)
        if at_ms <= 0:
            return None
        return WorkflowStep(
            kind=kind,
            at_ms=at_ms,
            title=str(
                row.get("intent_name")
                or row.get("model")
                or row.get("workflow_source")
                or row.get("agent_id")
                or kind
            ),
            status=str(
                row.get("route_status")
                or row.get("routing_reason")
                or row.get("workflow_step")
                or ""
            ),
            detail=str(row.get("diagnostic") or row.get("detail") or ""),
            trace_id=str(row.get("trace_id") or ""),
        )

    @staticmethod
    def _read_jsonl(path: Path, limit: int = 5000) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:  # noqa: BLE001
                continue
        return rows
