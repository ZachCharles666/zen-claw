"""Tests for B2 — Agent workflow path visualization."""

from __future__ import annotations

import json
from pathlib import Path

from zen_claw.telemetry.workflow_path import WorkflowPath, WorkflowPathBuilder, WorkflowStep

# ── helpers ───────────────────────────────────────────────────────────────────

def _write_event(log_path: Path, row: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _intent_event(trace_id: str, at_ms: int, intent: str = "TIME", status: str = "direct_success") -> dict:
    return {
        "at_ms": at_ms,
        "trace_id": trace_id,
        "intent_name": intent,
        "route_status": status,
        "diagnostic": "",
    }


def _model_event(trace_id: str, at_ms: int, model: str = "claude-haiku-4-5") -> dict:
    return {
        "at_ms": at_ms,
        "trace_id": trace_id,
        "model": model,
        "routing_reason": "cost_model",
    }


# ── WorkflowStep ──────────────────────────────────────────────────────────────

def test_step_to_dict():
    step = WorkflowStep(kind="intent_router", at_ms=1000, title="TIME", status="direct_success")
    d = step.to_dict()
    assert d["kind"] == "intent_router"
    assert d["title"] == "TIME"
    assert d["at_ms"] == 1000


# ── WorkflowPath ──────────────────────────────────────────────────────────────

def test_path_to_dict_empty():
    path = WorkflowPath(trace_id="abc123")
    d = path.to_dict()
    assert d["trace_id"] == "abc123"
    assert d["step_count"] == 0
    assert d["steps"] == []


# ── WorkflowPathBuilder ───────────────────────────────────────────────────────

def test_build_empty_when_no_logs(tmp_path: Path):
    builder = WorkflowPathBuilder(tmp_path)
    path = builder.build(trace_id="non-existent")
    assert path.steps == []


def test_build_empty_when_no_id(tmp_path: Path):
    builder = WorkflowPathBuilder(tmp_path)
    path = builder.build()
    assert path.steps == []


def test_build_finds_intent_step(tmp_path: Path):
    log = tmp_path / "dashboard" / "intent_router.log.jsonl"
    _write_event(log, _intent_event("trace1", at_ms=1000))
    _write_event(log, _intent_event("trace2", at_ms=2000))  # different trace

    builder = WorkflowPathBuilder(tmp_path)
    path = builder.build(trace_id="trace1")
    assert len(path.steps) == 1
    assert path.steps[0].kind == "intent_router"
    assert path.steps[0].title == "TIME"


def test_build_combines_multiple_log_sources(tmp_path: Path):
    intent_log = tmp_path / "dashboard" / "intent_router.log.jsonl"
    model_log = tmp_path / "dashboard" / "model_routing.log.jsonl"
    tid = "traceABC"
    _write_event(intent_log, _intent_event(tid, at_ms=1000))
    _write_event(model_log, _model_event(tid, at_ms=2000))

    builder = WorkflowPathBuilder(tmp_path)
    path = builder.build(trace_id=tid)
    assert len(path.steps) == 2
    kinds = {s.kind for s in path.steps}
    assert "intent_router" in kinds
    assert "model_routing" in kinds


def test_build_steps_sorted_chronologically(tmp_path: Path):
    log = tmp_path / "dashboard" / "intent_router.log.jsonl"
    tid = "chronoTrace"
    _write_event(log, _intent_event(tid, at_ms=3000, intent="WEATHER"))
    _write_event(log, _intent_event(tid, at_ms=1000, intent="TIME"))

    builder = WorkflowPathBuilder(tmp_path)
    path = builder.build(trace_id=tid)
    assert path.steps[0].at_ms == 1000
    assert path.steps[1].at_ms == 3000


def test_recent_returns_distinct_traces(tmp_path: Path):
    log = tmp_path / "dashboard" / "intent_router.log.jsonl"
    for i in range(3):
        _write_event(log, _intent_event(f"trace{i}", at_ms=1000 + i * 100))

    builder = WorkflowPathBuilder(tmp_path)
    paths = builder.recent(limit=10)
    trace_ids = {p.trace_id for p in paths}
    assert len(trace_ids) == 3
