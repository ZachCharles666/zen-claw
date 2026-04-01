"""Lightweight local dashboard server for operational visibility."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zen_claw.config.schema import Config

from zen_claw.channels.registry import build_channel_rbac_row, iter_channel_specs

try:
    from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    _HAS_FASTAPI = True
except Exception:
    _HAS_FASTAPI = False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _read_jsonl(path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                rows.append(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return rows[-limit:]


def _build_model_routing_summary(
    rows: list[dict[str, Any]] | None = None,
    *,
    bucket_minutes: int = 60,
    max_groups: int = 8,
) -> dict[str, Any]:
    events = [row for row in (rows or []) if isinstance(row, dict)]
    bounded_bucket_minutes = max(5, min(int(bucket_minutes or 60), 24 * 60))
    bucket_ms = bounded_bucket_minutes * 60 * 1000
    summary = {
        "total": len(events),
        "latest_model": str((events[0] or {}).get("selected_model") or "") if events else "",
        "latest_reason": str((events[0] or {}).get("reason") or "") if events else "",
        "bucket_minutes": bounded_bucket_minutes,
        "from_at_ms": int((events[-1] or {}).get("at_ms") or 0) if events else 0,
        "to_at_ms": int((events[0] or {}).get("at_ms") or 0) if events else 0,
    }
    model_counter = Counter(str(row.get("selected_model") or "").strip() or "unknown" for row in events)
    reason_counter = Counter(str(row.get("reason") or "").strip() or "unknown" for row in events)
    intent_counter = Counter(str(row.get("intent_name") or "").strip() or "unknown" for row in events)
    channel_counter = Counter(str(row.get("channel") or "").strip() or "unknown" for row in events)
    bucket_counter: Counter[int] = Counter()
    for row in events:
        at_ms = int(row.get("at_ms") or 0)
        if at_ms <= 0:
            continue
        bucket_counter[(at_ms // bucket_ms) * bucket_ms] += 1
    summary["models"] = [{"model": model, "count": count} for model, count in model_counter.most_common(max_groups)]
    summary["reasons"] = [
        {"reason": reason, "count": count} for reason, count in reason_counter.most_common(max_groups)
    ]
    summary["intents"] = [
        {"intent": intent, "count": count} for intent, count in intent_counter.most_common(max_groups)
    ]
    summary["channels"] = [
        {"channel": channel, "count": count} for channel, count in channel_counter.most_common(max_groups)
    ]
    summary["buckets"] = [
        {"bucket_at_ms": bucket_at_ms, "count": count}
        for bucket_at_ms, count in sorted(bucket_counter.items(), key=lambda item: item[0], reverse=True)[
            : max(1, max_groups)
        ]
    ]
    return summary


def _build_agent_execution_summary(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = [row for row in (rows or []) if isinstance(row, dict)]
    direct_paths = 0
    gate3_paths = 0
    clarifications = 0
    approval_waits = 0
    failed = 0
    reflected_runs = 0
    fallback_runs = 0
    reload_runs = 0
    total_reflections = 0
    tool_failures = 0
    for row in events:
        intent = row.get("execution_intent") if isinstance(row.get("execution_intent"), dict) else {}
        result = row.get("execution_result") if isinstance(row.get("execution_result"), dict) else {}
        trace_summary = (
            result.get("trace_summary") if isinstance(result.get("trace_summary"), dict) else {}
        )
        path_type = str(intent.get("path_type") or "").strip().lower()
        status = str(result.get("status") or "").strip().lower()
        if path_type == "direct":
            direct_paths += 1
        if path_type == "gate3_plan":
            gate3_paths += 1
        if path_type == "clarification":
            clarifications += 1
        if path_type == "approval_wait" or status == "approval_required":
            approval_waits += 1
        if status == "failed":
            failed += 1
        if bool(trace_summary.get("reflection_triggered")):
            reflected_runs += 1
        if bool(trace_summary.get("fallback_used")):
            fallback_runs += 1
        if bool(trace_summary.get("reload_triggered")):
            reload_runs += 1
        total_reflections += max(0, int(trace_summary.get("reflections_used") or 0))
        tool_failures += max(0, int(trace_summary.get("tool_failures") or 0))
    latest = events[0] if events else {}
    latest_result = latest.get("execution_result") if isinstance(latest.get("execution_result"), dict) else {}
    latest_intent = latest.get("execution_intent") if isinstance(latest.get("execution_intent"), dict) else {}
    latest_trace = (
        latest_result.get("trace_summary")
        if isinstance(latest_result.get("trace_summary"), dict)
        else {}
    )
    return {
        "total": len(events),
        "direct_paths": direct_paths,
        "gate3_paths": gate3_paths,
        "clarifications": clarifications,
        "approval_waits": approval_waits,
        "reflected_runs": reflected_runs,
        "fallback_runs": fallback_runs,
        "reload_runs": reload_runs,
        "total_reflections": total_reflections,
        "tool_failures": tool_failures,
        "delegated": len(
            [
                row
                for row in events
                if str(
                    (
                        row.get("routing_decision")
                        if isinstance(row.get("routing_decision"), dict)
                        else {}
                    ).get("action")
                    or ""
                ).strip().lower()
                == "delegate"
            ]
        ),
        "failed": failed,
        "latest_status": str(latest_result.get("status") or "") if latest_result else "",
        "latest_path_type": str(latest_intent.get("path_type") or ""),
        "latest_failure_classification": str(latest_result.get("failure_classification") or ""),
        "latest_fallback_reason": str(latest_trace.get("fallback_reason") or ""),
    }


def _build_agent_execution_activity_rows(
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        routing = row.get("routing_decision") if isinstance(row.get("routing_decision"), dict) else {}
        intent = row.get("execution_intent") if isinstance(row.get("execution_intent"), dict) else {}
        result = row.get("execution_result") if isinstance(row.get("execution_result"), dict) else {}
        trace_summary = (
            result.get("trace_summary") if isinstance(result.get("trace_summary"), dict) else {}
        )
        status = str(result.get("status") or "").strip().lower()
        routing_action = str(routing.get("action") or "").strip().lower()
        if status == "failed":
            action = "execution_failed"
        elif status == "approval_required":
            action = "approval_wait"
        elif status == "clarified":
            action = "clarification_requested"
        elif routing_action == "delegate":
            action = "routing_delegate"
        else:
            action = "execution_succeeded"
        path_type = str(intent.get("path_type") or "").strip()
        execution_mode = str(intent.get("execution_mode") or "").strip()
        decision_source = str(intent.get("decision_source") or "").strip()
        failure_classification = str(result.get("failure_classification") or "").strip()
        detail_parts = [
            f"path={path_type or '-'}",
            f"mode={execution_mode or '-'}",
            f"source={decision_source or '-'}",
            f"status={status or '-'}",
        ]
        if failure_classification:
            detail_parts.append(f"failure={failure_classification}")
        reflections_used = max(0, int(trace_summary.get("reflections_used") or 0))
        tool_failures = max(0, int(trace_summary.get("tool_failures") or 0))
        if reflections_used:
            detail_parts.append(f"reflections={reflections_used}")
        if tool_failures:
            detail_parts.append(f"tool_failures={tool_failures}")
        if bool(trace_summary.get("fallback_used")):
            detail_parts.append(
                f"fallback={str(trace_summary.get('fallback_reason') or 'used').strip() or 'used'}"
            )
        if bool(trace_summary.get("reload_triggered")):
            detail_parts.append("reload_triggered=true")
        if bool(trace_summary.get("approval_waited")):
            detail_parts.append("approval_waited=true")
        out.append(
            _to_control_plane_row(
                surface="agent_execution",
                target=str(row.get("intent_name") or intent.get("contract_intent") or ""),
                action=action,
                actor="",
                detail="; ".join(detail_parts),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=False,
            )
        )
    return out


_BUCKET_UNIT_MS: dict[str, int] = {
    "hour": 3_600_000,
    "day": 86_400_000,
    "week": 604_800_000,
}


def _build_model_routing_deep_summary(
    rows: list[dict[str, Any]] | None = None,
    *,
    bucket_unit: str = "hour",
    max_groups: int = 10,
) -> dict[str, Any]:
    """Return a richer model-routing aggregation with cross-dimension pivot and trend.

    Unlike :func:`_build_model_routing_summary` (which only produces flat counters),
    this function also builds:

    * **pivot** — for every top-N model, a breakdown of its associated reasons /
      intents / channels.
    * **trend** — time-bucketed counts (by *bucket_unit*) with per-bucket model
      distribution.
    """
    events = [row for row in (rows or []) if isinstance(row, dict)]
    unit = str(bucket_unit or "hour").strip().lower()
    if unit not in _BUCKET_UNIT_MS:
        unit = "hour"
    bucket_ms = _BUCKET_UNIT_MS[unit]
    bounded_max = max(1, min(int(max_groups or 10), 50))

    model_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    intent_counter: Counter[str] = Counter()
    channel_counter: Counter[str] = Counter()
    # per-model sub-counters for pivot
    model_reason: dict[str, Counter[str]] = {}
    model_intent: dict[str, Counter[str]] = {}
    model_channel: dict[str, Counter[str]] = {}
    # per-bucket counters for trend
    bucket_total: Counter[int] = Counter()
    bucket_models: dict[int, Counter[str]] = {}

    for row in events:
        model = str(row.get("selected_model") or "").strip() or "unknown"
        reason = str(row.get("reason") or "").strip() or "unknown"
        intent = str(row.get("intent_name") or "").strip() or "unknown"
        channel = str(row.get("channel") or "").strip() or "unknown"
        at_ms = int(row.get("at_ms") or 0)

        model_counter[model] += 1
        reason_counter[reason] += 1
        intent_counter[intent] += 1
        channel_counter[channel] += 1

        if model not in model_reason:
            model_reason[model] = Counter()
            model_intent[model] = Counter()
            model_channel[model] = Counter()
        model_reason[model][reason] += 1
        model_intent[model][intent] += 1
        model_channel[model][channel] += 1

        if at_ms > 0:
            bucket_key = (at_ms // bucket_ms) * bucket_ms
            bucket_total[bucket_key] += 1
            if bucket_key not in bucket_models:
                bucket_models[bucket_key] = Counter()
            bucket_models[bucket_key][model] += 1

    # Build flat lists
    top_models = model_counter.most_common(bounded_max)
    pivot: dict[str, Any] = {}
    for model, _ in top_models:
        pivot[model] = {
            "reasons": [
                {"reason": r, "count": c}
                for r, c in model_reason.get(model, Counter()).most_common(bounded_max)
            ],
            "intents": [
                {"intent": i, "count": c}
                for i, c in model_intent.get(model, Counter()).most_common(bounded_max)
            ],
            "channels": [
                {"channel": ch, "count": c}
                for ch, c in model_channel.get(model, Counter()).most_common(bounded_max)
            ],
        }

    trend = [
        {
            "bucket_at_ms": bk,
            "count": bucket_total[bk],
            "models": [
                {"model": m, "count": c}
                for m, c in bucket_models.get(bk, Counter()).most_common(bounded_max)
            ],
        }
        for bk in sorted(bucket_total.keys(), reverse=True)[:bounded_max]
    ]

    from_at_ms = int(events[-1].get("at_ms") or 0) if events else 0
    to_at_ms = int(events[0].get("at_ms") or 0) if events else 0

    return {
        "total": len(events),
        "from_at_ms": from_at_ms,
        "to_at_ms": to_at_ms,
        "bucket_unit": unit,
        "bucket_ms": bucket_ms,
        "models": [{"model": m, "count": c} for m, c in top_models],
        "reasons": [{"reason": r, "count": c} for r, c in reason_counter.most_common(bounded_max)],
        "intents": [{"intent": i, "count": c} for i, c in intent_counter.most_common(bounded_max)],
        "channels": [{"channel": ch, "count": c} for ch, c in channel_counter.most_common(bounded_max)],
        "pivot": pivot,
        "trend": trend,
    }


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _verify_approval_chain(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify approval event hash chain integrity (signature excluded)."""
    prev_hash = ""
    checked = 0
    for idx, row in enumerate(events):
        if not isinstance(row, dict):
            return {
                "ok": False,
                "checked": checked,
                "error_index": idx,
                "error": "invalid event row",
            }
        if str(row.get("prev_hash") or "") != prev_hash:
            return {
                "ok": False,
                "checked": checked,
                "error_index": idx,
                "error": "approval chain broken",
            }
        base_event = {
            "event_id": str(row.get("event_id") or ""),
            "task_id": str(row.get("task_id") or ""),
            "node_id": str(row.get("node_id") or ""),
            "action": str(row.get("action") or ""),
            "actor": str(row.get("actor") or ""),
            "note": str(row.get("note") or ""),
            "at_ms": int(row.get("at_ms") or 0),
            "prev_hash": str(row.get("prev_hash") or ""),
        }
        canonical = json.dumps(
            base_event, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if str(row.get("hash") or "") != expected_hash:
            return {
                "ok": False,
                "checked": checked,
                "error_index": idx,
                "error": "approval hash mismatch",
            }
        prev_hash = expected_hash
        checked += 1
    return {"ok": True, "checked": checked, "error_index": None, "error": ""}


def _build_recent_observability_events(
    approval_timeline: list[dict[str, Any]],
    compression_events: list[dict[str, Any]],
    intent_router_events: list[dict[str, Any]],
    workflow_webhook_events: list[dict[str, Any]],
    model_routing_events: list[dict[str, Any]],
    knowledge_cron_events: list[dict[str, Any]],
    policy_audit_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in approval_timeline[:20]:
        rows.append(
            {
                "kind": "approval",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("action") or "approval"),
                "status": str(event.get("action") or ""),
                "trace_id": "",
                "detail": (
                    f"node={str(event.get('node_id') or '-')}; "
                    f"task={str(event.get('task_id') or '-')}; "
                    f"actor={str(event.get('actor') or '-')}"
                ),
            }
        )
    for event in compression_events[:20]:
        rows.append(
            {
                "kind": "compression",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("session") or "compression"),
                "status": str(event.get("reason") or ""),
                "trace_id": "",
                "detail": f"turn={str(event.get('at_turn') or '-')}",
            }
        )
    for event in intent_router_events[:20]:
        rows.append(
            {
                "kind": "intent_router",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("intent_name") or "intent"),
                "status": str(event.get("route_status") or ""),
                "trace_id": str(event.get("trace_id") or ""),
                "detail": str(event.get("diagnostic") or ""),
            }
        )
    for event in workflow_webhook_events[:20]:
        rows.append(
            {
                "kind": "workflow_webhook",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("workflow_source") or event.get("agent_id") or "workflow"),
                "status": str(event.get("workflow_step") or ""),
                "trace_id": str(event.get("trace_id") or ""),
                "detail": (
                    f"agent={str(event.get('agent_id') or '-')}; "
                    f"run={str(event.get('workflow_run_id') or '-')}"
                ),
            }
        )
    for event in model_routing_events[:20]:
        rows.append(
            {
                "kind": "model_routing",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("selected_model") or "model"),
                "status": str(event.get("reason") or ""),
                "trace_id": str(event.get("trace_id") or ""),
                "detail": str(event.get("intent_name") or ""),
            }
        )
    for event in knowledge_cron_events[:20]:
        rows.append(
            {
                "kind": "knowledge_cron",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("job_name") or event.get("knowledge_notebook") or "knowledge"),
                "status": str(event.get("status") or ""),
                "trace_id": "",
                "detail": (
                    f"notebook={str(event.get('knowledge_notebook') or '-')}; "
                    f"chunks={str(event.get('chunks_added') or 0)}"
                ),
            }
        )
    for event in policy_audit_events[:20]:
        rows.append(
            {
                "kind": "policy_audit",
                "at_ms": event.get("at_ms"),
                "title": str(event.get("policy_kind") or "policy"),
                "status": str(event.get("target_id") or ""),
                "trace_id": str(event.get("trace_id") or ""),
                "detail": (
                    f"tenant={str(event.get('tenant_id') or '-')}; "
                    f"backend={str(event.get('after_store_backend') or 'default')}; "
                    f"actor={str(event.get('actor') or '-')}"
                ),
            }
        )
    return sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)[:15]


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        return 0
    return total


def _build_dashboard_knowledge_backend_summary(
    *,
    data_dir: Path,
    config: "Config",
    knowledge_notebooks: list[dict[str, Any]],
) -> dict[str, Any]:
    configured_backend = str(getattr(config.knowledge, "store_backend", "") or "chroma").strip() or "chroma"
    chroma_subdir = (
        str(getattr(config.knowledge, "chroma_subdir", "") or "chroma").strip() or "chroma"
    )
    memory_namespace = str(getattr(config.knowledge, "memory_namespace", "") or "").strip()
    by_backend: dict[str, dict[str, Any]] = {}
    for row in knowledge_notebooks:
        backend = str(row.get("store_backend") or configured_backend).strip() or configured_backend
        summary = by_backend.setdefault(
            backend,
            {
                "backend": backend,
                "healthy": True,
                "notebook_count": 0,
                "document_count": 0,
                "storage_present": False,
                "storage_bytes": 0,
                "storage_path": "",
                "namespace": "",
            },
        )
        summary["notebook_count"] = int(summary["notebook_count"]) + 1
        summary["document_count"] = int(summary["document_count"]) + max(0, int(row.get("doc_count") or 0))
    if "chroma" in by_backend:
        chroma_path = data_dir / "knowledge" / chroma_subdir
        by_backend["chroma"]["storage_path"] = str(chroma_path)
        by_backend["chroma"]["storage_present"] = chroma_path.exists()
        by_backend["chroma"]["storage_bytes"] = _dir_size_bytes(chroma_path) if chroma_path.exists() else 0
        by_backend["chroma"]["healthy"] = bool(by_backend["chroma"]["storage_present"]) or (
            int(by_backend["chroma"]["document_count"]) == 0
        )
    if "memory" in by_backend:
        by_backend["memory"]["namespace"] = memory_namespace
        by_backend["memory"]["healthy"] = True
    detected_backends = sorted(by_backend.values(), key=lambda row: str(row.get("backend") or ""))
    return {
        "configured_backend": configured_backend,
        "memory_namespace": memory_namespace,
        "detected_backends": detected_backends,
        "all_healthy": all(bool(row.get("healthy", False)) for row in detected_backends)
        if detected_backends
        else True,
    }


def build_dashboard_snapshot(config: "Config") -> dict[str, Any]:
    """Build dashboard status payload from config and runtime state files."""
    from zen_claw.agent.pool import list_agent_profiles
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir
    from zen_claw.runtime.sidecar_supervisor import collect_sidecar_status

    data_dir = get_data_dir()
    now_ms = int(time.time() * 1000)
    try:
        skills_loader = SkillsLoader(config.workspace_path)
        skills_inventory = skills_loader.build_skills_inventory()
    except Exception:
        skills_inventory = {
            "schema": "zen-claw.skills.inventory.v1",
            "skills_count": 0,
            "enabled_count": 0,
            "available_count": 0,
            "enforce_ready_count": 0,
            "invalid_count": 0,
            "missing_manifest_count": 0,
            "skills": [],
        }

    cron_data = _read_json(data_dir / "cron" / "jobs.json")
    cron_jobs = cron_data.get("jobs", []) if isinstance(cron_data.get("jobs"), list) else []
    cron_total = len(cron_jobs)
    cron_enabled = len([j for j in cron_jobs if bool(j.get("enabled", True))])
    cron_failures = len(
        [
            j
            for j in cron_jobs
            if isinstance(j.get("state"), dict)
            and str(j["state"].get("lastStatus") or "") == "error"
        ]
    )

    def _is_knowledge_related_cron(job: dict[str, Any]) -> bool:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if str(payload.get("knowledgeSource") or "").strip():
            return True
        name = str(job.get("name") or "").lower()
        message = str(payload.get("message") or "").lower()
        target_url = str(payload.get("targetUrl") or "").lower()
        haystack = " ".join([name, message, target_url])
        keywords = ("knowledge", "rag", "ingest", "notebook", "chat/upload")
        return any(keyword in haystack for keyword in keywords)

    def _cron_target_kind(job: dict[str, Any]) -> str:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if str(payload.get("targetUrl") or "").strip():
            return "webhook"
        if bool(payload.get("deliver", False)):
            return "channel_delivery"
        return "agent_turn"

    def _cron_job_row(job: dict[str, Any]) -> dict[str, Any]:
        state = job.get("state") if isinstance(job.get("state"), dict) else {}
        sched = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        return {
            "id": str(job.get("id") or ""),
            "name": str(job.get("name") or ""),
            "enabled": bool(job.get("enabled", True)),
            "schedule_kind": str(sched.get("kind") or ""),
            "target_kind": _cron_target_kind(job),
            "knowledge_related": _is_knowledge_related_cron(job),
            "next_run_at_ms": state.get("nextRunAtMs"),
            "last_status": state.get("lastStatus"),
            "last_error": state.get("lastError"),
        }

    cron_rows = [_cron_job_row(j) for j in cron_jobs][:50]

    knowledge_index = _read_json(data_dir / "knowledge" / "notebooks.json")
    knowledge_notebooks_raw = (
        knowledge_index.get("notebooks", [])
        if isinstance(knowledge_index.get("notebooks"), list)
        else []
    )
    knowledge_notebooks = [row for row in knowledge_notebooks_raw if isinstance(row, dict)]
    try:
        from zen_claw.auth.tenant import TenantStore

        tenant_rows = [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "enabled": bool(tenant.enabled),
                "store_backend": str(getattr(tenant, "store_backend", "") or ""),
            }
            for tenant in TenantStore(data_dir).list()
        ]
    except Exception:
        tenant_rows = []
    knowledge_rows = sorted(
        [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "doc_count": int(row.get("doc_count") or 0),
                "created_at": str(row.get("created_at") or ""),
                "store_backend": str(row.get("store_backend") or ""),
            }
            for row in knowledge_notebooks
        ],
        key=lambda row: (row["name"].lower(), row["id"]),
    )[:20]
    knowledge_summary = {
        "total_notebooks": len(knowledge_notebooks),
        "total_documents": sum(max(0, int(row.get("doc_count") or 0)) for row in knowledge_notebooks),
        "non_empty_notebooks": len(
            [row for row in knowledge_notebooks if int(row.get("doc_count") or 0) > 0]
        ),
        "chroma_store_present": (data_dir / "knowledge" / "chroma").exists(),
        "notebooks": knowledge_rows,
        "tenant_policies": sorted(
            tenant_rows,
            key=lambda row: (row["name"].lower(), row["tenant_id"]),
        )[:20],
    }
    knowledge_summary["backend_diagnostics"] = _build_dashboard_knowledge_backend_summary(
        data_dir=data_dir,
        config=config,
        knowledge_notebooks=knowledge_notebooks,
    )
    knowledge_cron_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "knowledge_cron.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    knowledge_summary["recent_cron_runs"] = knowledge_cron_rows[:5]
    knowledge_summary["cron_runs_total"] = len(knowledge_cron_rows)
    knowledge_summary["cron_runs_ok"] = len(
        [row for row in knowledge_cron_rows if str(row.get("status") or "") == "ok"]
    )
    knowledge_summary["cron_runs_error"] = len(
        [row for row in knowledge_cron_rows if str(row.get("status") or "") == "error"]
    )
    knowledge_policy_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "knowledge_policy.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    knowledge_summary["recent_policy_changes"] = knowledge_policy_rows[:5]
    knowledge_summary["policy_changes_total"] = len(knowledge_policy_rows)
    skill_action_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "skills_actions.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    skill_check_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "skills_checks.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    skill_export_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    latest_skill_checks: dict[str, dict[str, Any]] = {}
    for row in skill_check_rows:
        skill_name = str(row.get("skill_name") or "").strip()
        if skill_name and skill_name not in latest_skill_checks:
            latest_skill_checks[skill_name] = row
    channel_action_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "channel_actions.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    channel_apply_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "channel_apply.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    pending_channel_actions = [
        row
        for row in channel_action_rows
        if int(row.get("at_ms") or 0)
        > _latest_apply_at_ms(
            apply_rows=channel_apply_rows,
            target=str(row.get("channel_name") or ""),
        )
    ]
    try:
        from zen_claw.skills.crawler.scheduler import load_sources_json, resolve_sources_path

        crawler_catalog_path = resolve_sources_path(data_dir)
        crawler_sources = load_sources_json(crawler_catalog_path) if crawler_catalog_path.exists() else []
    except Exception:
        crawler_catalog_path = data_dir / "crawler" / "sources.json"
        crawler_sources = []
    crawler_source_rows = [
        {
            "name": source.name,
            "url": source.url,
            "notebook_id": source.notebook_id,
            "selector": source.selector,
            "use_browser": bool(source.use_browser),
            "max_chars": int(source.max_chars),
            "metadata": dict(source.metadata or {}),
            "enabled": bool(source.enabled),
        }
        for source in crawler_sources
    ]
    crawler_source_action_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    crawler_run_rows = _read_recent_crawler_runs(data_dir=data_dir, limit=50)[:10]

    rate_stats = _read_json(data_dir / "channels" / "rate_limit_stats.json")
    runtime_channels = rate_stats.get("channels", {}) if isinstance(rate_stats, dict) else {}
    rate_runtime_rows: list[dict[str, Any]] = []
    if isinstance(runtime_channels, dict):
        for name, row in sorted(runtime_channels.items()):
            if not isinstance(row, dict):
                continue
            rate_runtime_rows.append(
                {
                    "channel": str(name),
                    "delayed_count": int(row.get("delayed_count", 0)),
                    "dropped_count": int(row.get("dropped_count", 0)),
                    "last_delay_ms": int(row.get("last_delay_ms", 0)),
                }
            )

    channel_rows: list[dict[str, Any]] = [
        {
            "name": row["name"],
            "display_name": row["display_name"],
            "enabled": row["enabled"],
            "rbac_enabled": row["rbac_enabled"],
            "admins": row["admins"],
            "users": row["users"],
            "transport": row["transport"],
            "inbound_mode": row["inbound_mode"],
            "webhook_backed": row["webhook_backed"],
            "passive_capable": row["passive_capable"],
            "verify_mode": row["verify_mode"],
            "verify_supported": row["verify_supported"],
            "media_mode": row["media_mode"],
            "media_supported": row["media_supported"],
            "error_mode": row["error_mode"],
            "runtime_visibility_supported": row["runtime_visibility_supported"],
            "runtime_control_mode": row["runtime_control_mode"],
            "alpha_contract_capabilities": list(row.get("alpha_contract_capabilities") or []),
            "alpha_contract_support": dict(row.get("alpha_contract_support") or {}),
            "alpha_contract_missing": list(row.get("alpha_contract_missing") or []),
            "alpha_contract_ready": bool(row.get("alpha_contract_ready")),
            "configuration": row["configuration"],
        }
        for row in (build_channel_rbac_row(config, spec) for spec in iter_channel_specs())
    ]

    node_data = _read_json(data_dir / "nodes" / "state.json")
    nodes = node_data.get("nodes", {}) if isinstance(node_data.get("nodes"), dict) else {}
    tasks = node_data.get("tasks", []) if isinstance(node_data.get("tasks"), list) else []
    approval_events = (
        node_data.get("approval_events", [])
        if isinstance(node_data.get("approval_events"), list)
        else []
    )
    approval_chain = _verify_approval_chain([e for e in approval_events if isinstance(e, dict)])

    pending_approval = 0
    pending_approval_overdue = 0
    timeout_rejected = 0
    queue_pending = 0
    queue_running = 0
    queue_failed = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status") or "").strip().lower()
        if status in {"pending", "pending_approval"}:
            queue_pending += 1
        if status in {"leased", "running"}:
            queue_running += 1
        if status in {"rejected", "error"}:
            queue_failed += 1
        if status == "pending_approval":
            pending_approval += 1
            approval = t.get("approval") if isinstance(t.get("approval"), dict) else {}
            expires_at = approval.get("expires_at_ms")
            if isinstance(expires_at, int) and expires_at > 0 and expires_at < now_ms:
                pending_approval_overdue += 1
        if status == "rejected" and str(t.get("error") or "").strip().lower() == "approval timeout":
            timeout_rejected += 1

    latest_task_by_node: dict[str, dict[str, Any]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        node_id = str(t.get("node_id") or "")
        if not node_id:
            continue
        cur = latest_task_by_node.get(node_id)
        cur_ms = (
            int(cur.get("updated_at_ms") or cur.get("created_at_ms") or 0)
            if isinstance(cur, dict)
            else -1
        )
        row_ms = int(t.get("updated_at_ms") or t.get("created_at_ms") or 0)
        if row_ms >= cur_ms:
            latest_task_by_node[node_id] = t

    node_rows: list[dict[str, Any]] = []
    for node_id, node in list(nodes.items())[:50]:
        if not isinstance(node, dict):
            continue
        policy = node.get("policy") if isinstance(node.get("policy"), dict) else {}
        latest_task = latest_task_by_node.get(str(node_id), {})
        node_rows.append(
            {
                "node_id": str(node_id),
                "name": str(node.get("name") or ""),
                "platform": str(node.get("platform") or ""),
                "status": str(node.get("status") or ""),
                "last_seen_ms": node.get("last_seen_ms"),
                "allow_gateway_tasks": bool(policy.get("allow_gateway_tasks", True)),
                "max_running_tasks": max(1, int(policy.get("max_running_tasks", 1) or 1)),
                "approval_required_count": max(
                    1, int(policy.get("approval_required_count", 1) or 1)
                ),
                "latest_task_type": str(latest_task.get("task_type") or ""),
                "latest_task_status": str(latest_task.get("status") or ""),
                "latest_task_updated_at_ms": latest_task.get("updated_at_ms"),
            }
        )
    node_rows.sort(key=lambda x: (x.get("name") or "", x.get("node_id") or ""))

    approval_timeline: list[dict[str, Any]] = []
    for e in sorted(
        [r for r in approval_events if isinstance(r, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:30]:
        approval_timeline.append(
            {
                "event_id": str(e.get("event_id") or ""),
                "at_ms": e.get("at_ms"),
                "node_id": str(e.get("node_id") or ""),
                "task_id": str(e.get("task_id") or ""),
                "action": str(e.get("action") or ""),
                "actor": str(e.get("actor") or ""),
                "note": str(e.get("note") or ""),
            }
        )

    provider_rows: list[dict[str, Any]] = []
    compression_events: list[dict[str, Any]] = []
    router_events = _read_jsonl(data_dir / "dashboard" / "intent_router.log.jsonl", limit=50)
    intent_router_events = sorted(
        [row for row in router_events if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:20]
    execution_events = _read_jsonl(data_dir / "dashboard" / "agent_execution.log.jsonl", limit=50)
    agent_execution_events = sorted(
        [row for row in execution_events if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:20]
    router_summary = {
        "total": len(intent_router_events),
        "direct_success": len(
            [row for row in intent_router_events if str(row.get("route_status") or "") == "direct_success"]
        ),
        "direct_failed": len(
            [row for row in intent_router_events if str(row.get("route_status") or "") == "direct_failed"]
        ),
        "replan": len(
            [
                row
                for row in intent_router_events
                if str(row.get("route_status") or "") == "needs_constrained_replan"
            ]
        ),
    }
    execution_summary = _build_agent_execution_summary(agent_execution_events)
    try:
        sessions_dir = Path.home() / ".zen-claw" / "sessions"
        for sp in sorted(
            sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:20]:
            try:
                first = sp.read_text(encoding="utf-8").splitlines()[0]
                row = json.loads(first)
                meta = row.get("metadata") if isinstance(row, dict) else {}
                events = meta.get("compression_events") if isinstance(meta, dict) else None
                if isinstance(events, list):
                    for ev in events[-5:]:
                        if isinstance(ev, dict):
                            compression_events.append(
                                {
                                    "session": sp.stem,
                                    "at_ms": ev.get("at_ms"),
                                    "at_turn": ev.get("at_turn"),
                                    "reason": ev.get("reason"),
                                }
                            )
            except Exception:
                continue
    except Exception:
        compression_events = []
    compression_events = sorted(
        compression_events,
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:30]
    workflow_events = _read_jsonl(data_dir / "dashboard" / "workflow_webhook.log.jsonl", limit=50)
    workflow_webhook_events = sorted(
        [row for row in workflow_events if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:20]
    workflow_webhook_summary = {
        "total": len(workflow_webhook_events),
        "with_trace": len(
            [row for row in workflow_webhook_events if str(row.get("trace_id") or "").strip()]
        ),
        "with_source": len(
            [row for row in workflow_webhook_events if str(row.get("workflow_source") or "").strip()]
        ),
        "source_counts": [
            {"source": source, "count": count}
            for source, count in Counter(
                str(row.get("workflow_source") or "").strip() or "unknown"
                for row in workflow_webhook_events
            ).most_common(5)
        ],
    }
    model_events = _read_jsonl(data_dir / "dashboard" / "model_routing.log.jsonl", limit=50)
    model_routing_events = sorted(
        [row for row in model_events if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:20]
    model_routing_summary = _build_model_routing_summary(
        model_routing_events,
        bucket_minutes=60,
        max_groups=8,
    )
    recent_observability_events = _build_recent_observability_events(
        approval_timeline,
        compression_events,
        intent_router_events,
        workflow_webhook_events,
        model_routing_events,
        knowledge_cron_rows,
        knowledge_policy_rows,
    )

    for name in [
        "openrouter",
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "zhipu",
        "dashscope",
        "moonshot",
        "groq",
        "aihubmix",
        "vllm",
    ]:
        p = getattr(config.providers, name)
        provider_rows.append(
            {
                "name": name,
                "api_key_set": bool(str(p.api_key or "").strip()),
            }
        )
    agent_profiles = list_agent_profiles(config)
    agent_action_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "agent_actions.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    agent_apply_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "agent_apply.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    pending_agent_actions = [
        row
        for row in agent_action_rows
        if int(row.get("at_ms") or 0)
        > _latest_apply_at_ms(
            apply_rows=agent_apply_rows,
            target=str(row.get("agent_id") or ""),
        )
    ]
    agent_profile_rows = [
        {
            "agent_id": row.agent_id,
            "display_name": row.display_name,
            "description": row.description,
            "model": row.model,
            "vision_model": row.vision_model,
            "thinking_model": row.thinking_model,
            "fallback_model": row.fallback_model,
            "cost_model": row.cost_model,
            "stability_model": row.stability_model,
            "planning_enabled": bool(row.enable_planning),
            "workspace": str(row.workspace),
            "registered": bool(row.is_registered),
            "intent_model_overrides": dict(row.intent_model_overrides or {}),
            "task_type_model_overrides": dict(row.task_type_model_overrides or {}),
            "allowed_models": list(row.allowed_models or []),
            "routing_keywords": list(
                getattr(config.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
            ),
            "routing_keywords_count": len(
                getattr(config.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
            ),
            "skill_names": list(row.skill_names or []),
            "skill_count": len(row.skill_names or []),
            "allowed_tools_count": len(row.allowed_tools or []),
            "denied_tools_count": len(row.denied_tools or []),
        }
        for row in agent_profiles
    ]
    ops_summary = {
        "status": "ok",
        "attention_items": 0,
        "agent_profile_total": len(agent_profile_rows),
        "agent_recent_actions": len(agent_action_rows),
        "agent_pending_reload": len(pending_agent_actions),
        "agent_execution_total": int(execution_summary.get("total") or 0),
        "agent_routing_delegated": int(execution_summary.get("delegated") or 0),
        "agent_execution_failed": int(execution_summary.get("failed") or 0),
        "agent_execution_approval_waits": int(execution_summary.get("approval_waits") or 0),
        "agent_execution_reflected": int(execution_summary.get("reflected_runs") or 0),
        "agent_execution_fallback": int(execution_summary.get("fallback_runs") or 0),
        "agent_execution_reload": int(execution_summary.get("reload_runs") or 0),
        "skill_failed_checks": len([row for row in skill_check_rows if not bool(row.get("ok"))]),
        "skill_recent_exports": len(skill_export_rows),
        "channel_pending_reload": len(pending_channel_actions),
        "model_routes": len(model_routing_events),
        "notes": [],
    }
    if ops_summary["skill_failed_checks"]:
        ops_summary["notes"].append(
            f"skills failed checks: {ops_summary['skill_failed_checks']}"
        )
    if ops_summary["channel_pending_reload"]:
        ops_summary["notes"].append(
            f"channels pending apply: {ops_summary['channel_pending_reload']}"
        )
    if ops_summary["agent_pending_reload"]:
        ops_summary["notes"].append(
            f"agents pending apply: {ops_summary['agent_pending_reload']}"
        )
    if ops_summary["agent_execution_failed"]:
        ops_summary["notes"].append(
            f"agent execution failures: {ops_summary['agent_execution_failed']}"
        )
    if ops_summary["agent_routing_delegated"]:
        ops_summary["notes"].append(
            f"agent routing delegated: {ops_summary['agent_routing_delegated']}"
        )
    if ops_summary["agent_execution_approval_waits"]:
        ops_summary["notes"].append(
            f"agent approval waits: {ops_summary['agent_execution_approval_waits']}"
        )
    if ops_summary["agent_execution_reflected"]:
        ops_summary["notes"].append(
            f"agent reflected runs: {ops_summary['agent_execution_reflected']}"
        )
    if ops_summary["agent_execution_fallback"]:
        ops_summary["notes"].append(
            f"agent fallback runs: {ops_summary['agent_execution_fallback']}"
        )
    if ops_summary["agent_execution_reload"]:
        ops_summary["notes"].append(
            f"agent reload-triggered runs: {ops_summary['agent_execution_reload']}"
        )
    if not ops_summary["model_routes"]:
        ops_summary["notes"].append("no recent model routing events")
    if not ops_summary["notes"]:
        ops_summary["notes"].append("operator surfaces nominal")
    ops_summary["attention_items"] = len(
        [note for note in ops_summary["notes"] if "nominal" not in note]
    )
    if ops_summary["attention_items"]:
        ops_summary["status"] = "attention"
    combined_pending_apply = sorted(
        [
            _to_control_plane_row(
                surface="agent",
                target=str(row.get("agent_id") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=True,
            )
            for row in pending_agent_actions
        ]
        + [
            _to_control_plane_row(
                surface="channel",
                target=str(row.get("channel_name") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=True,
            )
            for row in pending_channel_actions
        ],
        key=lambda row: int(row.get("at_ms") or 0),
        reverse=True,
    )
    annotated_pending_apply = [
        _annotate_control_plane_execution(row)
        for row in combined_pending_apply
        if isinstance(row, dict)
    ]
    combined_recent_activity = sorted(
        [
            _to_control_plane_row(
                surface="agent",
                target=str(row.get("agent_id") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=int(row.get("at_ms") or 0)
                > _latest_apply_at_ms(
                    apply_rows=agent_apply_rows,
                    target=str(row.get("agent_id") or ""),
                ),
            )
            for row in agent_action_rows[:10]
        ]
        + [
            _to_control_plane_row(
                surface="channel",
                target=str(row.get("channel_name") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=int(row.get("at_ms") or 0)
                > _latest_apply_at_ms(
                    apply_rows=channel_apply_rows,
                    target=str(row.get("channel_name") or ""),
                ),
            )
            for row in channel_action_rows[:10]
        ]
        + _build_agent_execution_activity_rows(agent_execution_events[:10])
        + [
            _to_control_plane_row(
                surface="skill",
                target=str(row.get("skill_name") or ""),
                action=(
                    "preflight_failed"
                    if row.get("event_kind") == "check" and row.get("ok") is False
                    else str(row.get("action") or row.get("event_kind") or "")
                ),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=False,
            )
            for row in (
                _read_skill_activity_history(data_dir=data_dir, limit=10, offset=0).get("rows") or []
            )
            if isinstance(row, dict)
        ]
        + [
            _to_control_plane_row(
                surface="model_routing",
                target=str(row.get("selected_model") or ""),
                action=str(row.get("reason") or ""),
                actor="",
                detail=str(row.get("intent_name") or row.get("channel") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=False,
            )
            for row in model_routing_events[:10]
        ],
        key=lambda row: int(row.get("at_ms") or 0),
        reverse=True,
    )[:8]
    supported_runtime_targets = {
        str(row.get("channel_name") or "").strip().lower()
        for row in list(
            (
                snapshot_row
                for snapshot_row in [_webchat_runtime_summary()]
                if isinstance(snapshot_row, dict)
            )
        )
        if bool(row.get("supported"))
    }
    failed_skill_rows = [
        _to_control_plane_row(
            surface="skill",
            target=str(row.get("skill_name") or ""),
            action="preflight_failed",
            actor=str(row.get("actor") or ""),
            detail=str(row.get("detail") or ""),
            trace_id=str(row.get("trace_id") or ""),
            at_ms=int(row.get("at_ms") or 0),
            pending_apply=False,
        )
        for row in skill_check_rows
        if isinstance(row, dict) and not bool(row.get("ok"))
    ]
    stale_pending_rows = [
        row
        for row in annotated_pending_apply
        if int(row.get("at_ms") or 0) > 0 and int(row.get("at_ms") or 0) <= (now_ms - (24 * 3600 * 1000))
    ]
    executable_pending_rows = [
        row
        for row in annotated_pending_apply
        if str(row.get("execution_mode") or "").strip().lower() == "runtime_apply_and_audit"
    ]
    audit_only_pending_rows = [
        row
        for row in annotated_pending_apply
        if str(row.get("execution_mode") or "").strip().lower() == "audit_only"
    ]
    unsupported_pending_rows = [
        row
        for row in annotated_pending_apply
        if str(row.get("capability_status") or "").strip().lower() in {"unsupported", "runtime_control_unsupported"}
    ]
    runtime_unsupported_rows = [
        _to_control_plane_row(
            surface="channel",
            target=str(row.get("name") or ""),
            action="runtime_apply_unsupported",
            actor="",
            detail="no in-process runtime/apply control",
            trace_id="",
            at_ms=now_ms,
            pending_apply=False,
        )
        for row in channel_rows
        if str(row.get("name") or "").strip().lower() not in supported_runtime_targets
    ]
    failed_execution_rows = [
        row for row in _build_agent_execution_activity_rows(agent_execution_events) if row["action"] == "execution_failed"
    ]
    delegated_execution_rows = [
        _to_control_plane_row(
            surface="agent_execution",
            target=str(row.get("intent_name") or ""),
            action="routing_delegate",
            actor="",
            detail=(
                "path="
                + str(
                    (
                        (
                            row.get("execution_intent")
                            if isinstance(row.get("execution_intent"), dict)
                            else {}
                        ).get("path_type")
                        or ""
                    ).strip()
                    or "-"
                )
                + "; mode="
                + str(
                    (
                        (
                            row.get("execution_intent")
                            if isinstance(row.get("execution_intent"), dict)
                            else {}
                        ).get("execution_mode")
                        or ""
                    ).strip()
                    or "-"
                )
                + "; source="
                + str(
                    (
                        (
                            row.get("execution_intent")
                            if isinstance(row.get("execution_intent"), dict)
                            else {}
                        ).get("decision_source")
                        or ""
                    ).strip()
                    or "-"
                )
            ),
            trace_id=str(row.get("trace_id") or ""),
            at_ms=int(row.get("at_ms") or 0),
            pending_apply=False,
        )
        for row in agent_execution_events
        if str(
            (
                row.get("routing_decision")
                if isinstance(row.get("routing_decision"), dict)
                else {}
            ).get("action")
            or ""
        ).strip().lower()
        == "delegate"
    ]
    attention_sections = [
        {
            "key": "failed_checks",
            "title": "Failed Checks",
            "count": len(failed_skill_rows),
            "rows": failed_skill_rows[:5],
        },
        {
            "key": "agent_execution_failed",
            "title": "Agent Execution Failed",
            "count": len(failed_execution_rows),
            "rows": failed_execution_rows[:5],
        },
        {
            "key": "agent_routing_delegate",
            "title": "Agent Routing Delegated",
            "count": len(delegated_execution_rows),
            "rows": delegated_execution_rows[:5],
        },
        {
            "key": "pending_apply",
            "title": "Pending Apply",
            "count": len(annotated_pending_apply),
            "rows": annotated_pending_apply[:5],
        },
        {
            "key": "pending_apply_executable",
            "title": "Executable Pending Apply",
            "count": len(executable_pending_rows),
            "rows": executable_pending_rows[:5],
        },
        {
            "key": "pending_apply_audit_only",
            "title": "Audit-only Pending Apply",
            "count": len(audit_only_pending_rows),
            "rows": audit_only_pending_rows[:5],
        },
        {
            "key": "pending_apply_unsupported",
            "title": "Unsupported Pending Apply",
            "count": len(unsupported_pending_rows),
            "rows": unsupported_pending_rows[:5],
        },
        {
            "key": "stale_pending_apply",
            "title": "Stale Pending Apply",
            "count": len(stale_pending_rows),
            "rows": stale_pending_rows[:5],
        },
        {
            "key": "runtime_unsupported",
            "title": "Runtime Unsupported",
            "count": len(runtime_unsupported_rows),
            "rows": runtime_unsupported_rows[:5],
        },
    ]
    ops_summary["attention_sections"] = [section for section in attention_sections if int(section["count"]) > 0]
    ops_summary["recent_activity"] = combined_recent_activity
    ops_summary["pending_apply_rows"] = annotated_pending_apply[:8]
    ops_summary["pending_apply_total"] = len(annotated_pending_apply)

    agent_config_state = {
        "model": config.agents.defaults.model,
        "vision_model": config.agents.defaults.vision_model or "",
        "memory_recall_mode": config.agents.defaults.memory_recall_mode,
        "planning_enabled": bool(config.agents.defaults.enable_planning),
        "max_reflections": int(config.agents.defaults.max_reflections),
        "compression_trigger_ratio": float(config.agents.defaults.compression_trigger_ratio),
        "compression_hysteresis_ratio": float(config.agents.defaults.compression_hysteresis_ratio),
        "compression_cooldown_turns": int(config.agents.defaults.compression_cooldown_turns),
    }
    agent_runtime_state = {
        "compression_events": compression_events,
        "model_routing_events": model_routing_events,
        "model_routing_summary": model_routing_summary,
        "intent_router_events": intent_router_events,
        "intent_router_summary": router_summary,
        "execution_events": agent_execution_events,
        "execution_summary": execution_summary,
        "workflow_webhook_events": workflow_webhook_events,
        "workflow_webhook_summary": workflow_webhook_summary,
        "recent_observability_events": recent_observability_events,
    }
    agent_operator_state = {
        "profiles": agent_profile_rows[:12],
        "profiles_total": len(agent_profile_rows),
        "registered_profiles": len([row for row in agent_profile_rows if row["registered"]]),
        "recent_actions": agent_action_rows[:5],
        "actions_total": len(agent_action_rows),
        "pending_reload": len(pending_agent_actions) > 0,
        "pending_reload_count": len(pending_agent_actions),
        "pending_reload_actions": pending_agent_actions[:5],
        "last_apply": agent_apply_rows[0] if agent_apply_rows else None,
    }

    return {
        "generated_at_ms": now_ms,
        "ops": ops_summary,
        "agent": {
            **agent_config_state,
            **agent_runtime_state,
            **agent_operator_state,
            "config_state": agent_config_state,
            "runtime_state": agent_runtime_state,
            "operator_state": agent_operator_state,
        },
        "security": {
            "production_hardening": bool(config.tools.policy.production_hardening),
            "subagent_hard_guardrail": not bool(config.tools.policy.allow_subagent_sensitive_tools),
            "skill_permissions_mode": str(config.agents.defaults.skill_permissions_mode),
        },
        "providers": provider_rows,
        "sidecars": collect_sidecar_status(config),
        "cron": {
            "total_jobs": cron_total,
            "enabled_jobs": cron_enabled,
            "failed_jobs": cron_failures,
            "webhook_jobs": len([j for j in cron_jobs if _cron_target_kind(j) == "webhook"]),
            "knowledge_related_jobs": len([j for j in cron_jobs if _is_knowledge_related_cron(j)]),
            "jobs": cron_rows,
        },
        "knowledge": knowledge_summary,
        "skills": {
            "total": int(skills_inventory.get("skills_count") or 0),
            "enabled": int(skills_inventory.get("enabled_count") or 0),
            "available": int(skills_inventory.get("available_count") or 0),
            "enforce_ready": int(skills_inventory.get("enforce_ready_count") or 0),
            "invalid": int(skills_inventory.get("invalid_count") or 0),
            "missing_manifest": int(skills_inventory.get("missing_manifest_count") or 0),
            "tested": int(skills_inventory.get("tested_count") or 0),
            "trusted": int(skills_inventory.get("trusted_count") or 0),
            "runtime_intent": int(skills_inventory.get("runtime_intent_count") or 0),
            "permissioned": int(skills_inventory.get("permissioned_count") or 0),
            "scoped": int(skills_inventory.get("scoped_count") or 0),
            "trust_breakdown": dict(skills_inventory.get("trust_breakdown") or {}),
            "runtime_mode_breakdown": dict(skills_inventory.get("runtime_mode_breakdown") or {}),
            "permission_breakdown": dict(skills_inventory.get("permission_breakdown") or {}),
            "scope_breakdown": dict(skills_inventory.get("scope_breakdown") or {}),
            "versioned_dirs": len(
                [
                    row
                    for row in list(skills_inventory.get("skills") or [])
                    if "_v" in str(row.get("physical_dir") or "")
                ]
            ),
                "journal_pending": len(
                    [
                        item
                        for item in list(skills_loader._load_journal() or [])
                        if isinstance(item, dict)
                    ]
                ),
            "recent_actions": skill_action_rows[:5],
            "actions_total": len(skill_action_rows),
            "recent_checks": skill_check_rows[:5],
            "checks_total": len(skill_check_rows),
            "failed_checks": len([row for row in skill_check_rows if not bool(row.get("ok"))]),
            "recent_exports": skill_export_rows[:5],
            "exports_total": len(skill_export_rows),
            "rows": [
                {
                    **row,
                    "last_check": latest_skill_checks.get(str(row.get("name") or "").strip()) or None,
                }
                for row in list(skills_inventory.get("skills") or [])[:12]
            ],
        },
        "crawler": {
            "catalog_path": str(crawler_catalog_path),
            "total_sources": len(crawler_source_rows),
            "browser_sources": len([row for row in crawler_source_rows if row["use_browser"]]),
            "recent_source_actions": crawler_source_action_rows[:5],
            "source_actions_total": len(crawler_source_action_rows),
            "recent_runs": crawler_run_rows[:5],
            "runs_total": len(crawler_run_rows),
            "failed_runs": len([row for row in crawler_run_rows if str(row.get("status") or "") == "error"]),
            "sources": crawler_source_rows[:12],
        },
        "channels": {
            "rows": channel_rows,
            "total": len(channel_rows),
            "enabled": len([row for row in channel_rows if row["enabled"]]),
            "alpha_ready": len([row for row in channel_rows if row.get("alpha_contract_ready")]),
            "alpha_gaps": len([row for row in channel_rows if not row.get("alpha_contract_ready")]),
            "webhook_backed": len([row for row in channel_rows if row["webhook_backed"]]),
            "passive_capable": len([row for row in channel_rows if row["passive_capable"]]),
            "runtime_controls": _build_channel_runtime_control_rows(config),
            "recent_actions": channel_action_rows[:5],
            "actions_total": len(channel_action_rows),
            "pending_reload": len(pending_channel_actions) > 0,
            "pending_reload_count": len(pending_channel_actions),
            "pending_reload_actions": pending_channel_actions[:5],
            "last_apply": channel_apply_rows[0] if channel_apply_rows else None,
        },
        "rate_limit": {
            "default": {
                "mode": config.channels.outbound_rate_limit_mode,
                "per_sec": float(config.channels.outbound_rate_limit_per_sec),
                "burst": int(config.channels.outbound_rate_limit_burst),
            },
            "runtime": rate_runtime_rows,
        },
        "node": {
            "total_nodes": len(nodes),
            "active_nodes": len(
                [
                    n
                    for n in nodes.values()
                    if isinstance(n, dict)
                    and str(n.get("status") or "").strip().lower() == "active"
                ]
            ),
            "total_tasks": len([t for t in tasks if isinstance(t, dict)]),
            "queue_pending": queue_pending,
            "queue_running": queue_running,
            "queue_failed": queue_failed,
            "pending_approval": pending_approval,
            "pending_approval_overdue": pending_approval_overdue,
            "approval_timeout_rejected": timeout_rejected,
            "approval_events": len([e for e in approval_events if isinstance(e, dict)]),
            "approval_chain_ok": bool(approval_chain.get("ok")),
            "approval_chain_checked": int(approval_chain.get("checked") or 0),
            "approval_chain_error": str(approval_chain.get("error") or ""),
            "nodes": node_rows,
            "approval_timeline": approval_timeline,
        },
    }


def _build_ops_summary_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    ops = snapshot.get("ops") if isinstance(snapshot.get("ops"), dict) else {}
    rows = [
        {"surface": "ops", "metric": "status", "value": str(ops.get("status") or "ok")},
        {
            "surface": "ops",
            "metric": "attention_items",
            "value": int(ops.get("attention_items") or 0),
        },
        {
            "surface": "agent",
            "metric": "profile_total",
            "value": int(ops.get("agent_profile_total") or 0),
        },
        {
            "surface": "agent",
            "metric": "recent_actions",
            "value": int(ops.get("agent_recent_actions") or 0),
        },
        {
            "surface": "agent",
            "metric": "pending_reload",
            "value": int(ops.get("agent_pending_reload") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "recent_total",
            "value": int(ops.get("agent_execution_total") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "delegated",
            "value": int(ops.get("agent_routing_delegated") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "failed",
            "value": int(ops.get("agent_execution_failed") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "approval_waits",
            "value": int(ops.get("agent_execution_approval_waits") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "reflected_runs",
            "value": int(ops.get("agent_execution_reflected") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "fallback_runs",
            "value": int(ops.get("agent_execution_fallback") or 0),
        },
        {
            "surface": "agent_execution",
            "metric": "reload_runs",
            "value": int(ops.get("agent_execution_reload") or 0),
        },
        {
            "surface": "skills",
            "metric": "failed_checks",
            "value": int(ops.get("skill_failed_checks") or 0),
        },
        {
            "surface": "skills",
            "metric": "recent_exports",
            "value": int(ops.get("skill_recent_exports") or 0),
        },
        {
            "surface": "channels",
            "metric": "pending_reload",
            "value": int(ops.get("channel_pending_reload") or 0),
        },
        {
            "surface": "model_routing",
            "metric": "recent_routes",
            "value": int(ops.get("model_routes") or 0),
        },
    ]
    return {
        "generated_at_ms": int(snapshot.get("generated_at_ms") or 0),
        "ops": ops,
        "rows": rows,
        "notes": [str(note or "") for note in (ops.get("notes") or []) if str(note or "").strip()],
        "attention_sections": list(ops.get("attention_sections") or []),
        "recent_activity": list(ops.get("recent_activity") or []),
        "pending_apply": list(ops.get("pending_apply_rows") or []),
        "pending_apply_total": int(ops.get("pending_apply_total") or 0),
    }


def _ops_summary_report_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["surface", "metric", "value"])
    for row in report.get("rows") or []:
        writer.writerow(
            [
                str(row.get("surface") or ""),
                str(row.get("metric") or ""),
                row.get("value") if row.get("value") is not None else "",
            ]
        )
    writer.writerow([])
    writer.writerow(["note_index", "note"])
    for idx, note in enumerate(report.get("notes") or [], start=1):
        writer.writerow([idx, str(note or "")])
    writer.writerow([])
    writer.writerow(
        ["attention_key", "attention_title", "attention_count", "surface", "at_ms", "target", "action", "actor", "detail", "trace_id", "pending_apply"]
    )
    for section in report.get("attention_sections") or []:
        for row in list(section.get("rows") or []):
            writer.writerow(
                [
                    str(section.get("key") or ""),
                    str(section.get("title") or ""),
                    int(section.get("count") or 0),
                    str(row.get("surface") or ""),
                    int(row.get("at_ms") or 0),
                    str(row.get("target") or ""),
                    str(row.get("action") or ""),
                    str(row.get("actor") or ""),
                    str(row.get("detail") or ""),
                    str(row.get("trace_id") or ""),
                    bool(row.get("pending_apply")),
                ]
            )
    writer.writerow([])
    writer.writerow(
        ["recent_surface", "at_ms", "target", "action", "actor", "detail", "trace_id", "pending_apply"]
    )
    for row in report.get("recent_activity") or []:
        writer.writerow(
            [
                str(row.get("surface") or ""),
                int(row.get("at_ms") or 0),
                str(row.get("target") or ""),
                str(row.get("action") or ""),
                str(row.get("actor") or ""),
                str(row.get("detail") or ""),
                str(row.get("trace_id") or ""),
                bool(row.get("pending_apply")),
            ]
        )
    writer.writerow([])
    writer.writerow(
        ["pending_surface", "at_ms", "target", "action", "actor", "detail", "trace_id", "pending_apply"]
    )
    for row in report.get("pending_apply") or []:
        writer.writerow(
            [
                str(row.get("surface") or ""),
                int(row.get("at_ms") or 0),
                str(row.get("target") or ""),
                str(row.get("action") or ""),
                str(row.get("actor") or ""),
                str(row.get("detail") or ""),
                str(row.get("trace_id") or ""),
                bool(row.get("pending_apply")),
            ]
        )
    return buf.getvalue()


def _to_control_plane_row(
    *,
    surface: str,
    target: str,
    action: str,
    actor: str,
    detail: str,
    trace_id: str,
    at_ms: int,
    pending_apply: bool,
) -> dict[str, Any]:
    return {
        "surface": str(surface or ""),
        "target": str(target or ""),
        "action": str(action or ""),
        "actor": str(actor or ""),
        "detail": str(detail or ""),
        "trace_id": str(trace_id or ""),
        "at_ms": int(at_ms or 0),
        "pending_apply": bool(pending_apply),
    }


def _build_control_plane_report(
    *,
    data_dir: Path,
    snapshot: dict[str, Any],
    surface: str = "",
    actor: str = "",
    target: str = "",
    pending_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    surface_filter = str(surface or "").strip().lower()
    actor_filter = str(actor or "").strip().lower()
    target_filter = str(target or "").strip().lower()
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))

    agent_actions = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "agent_actions.log.jsonl", limit=200)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    channel_actions = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "channel_actions.log.jsonl", limit=200)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    agent_apply_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "agent_apply.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    channel_apply_rows = sorted(
        [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "channel_apply.log.jsonl", limit=50)
            if isinstance(row, dict)
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for row in agent_actions:
        rows.append(
            _to_control_plane_row(
                surface="agent",
                target=str(row.get("agent_id") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=int(row.get("at_ms") or 0)
                > _latest_apply_at_ms(
                    apply_rows=agent_apply_rows,
                    target=str(row.get("agent_id") or ""),
                ),
            )
        )
    for row in channel_actions:
        rows.append(
            _to_control_plane_row(
                surface="channel",
                target=str(row.get("channel_name") or ""),
                action=str(row.get("action") or ""),
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or row.get("action") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=int(row.get("at_ms") or 0)
                > _latest_apply_at_ms(
                    apply_rows=channel_apply_rows,
                    target=str(row.get("channel_name") or ""),
                ),
            )
        )
    for row in _read_skill_activity_history(data_dir=data_dir, limit=100, offset=0).get("rows") or []:
        if not isinstance(row, dict):
            continue
        action_name = str(row.get("action") or row.get("event_kind") or "")
        if row.get("event_kind") == "check" and row.get("ok") is False:
            action_name = "preflight_failed"
        rows.append(
            _to_control_plane_row(
                surface="skill",
                target=str(row.get("skill_name") or ""),
                action=action_name,
                actor=str(row.get("actor") or ""),
                detail=str(row.get("detail") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=False,
            )
        )
    for row in _read_jsonl(data_dir / "dashboard" / "model_routing.log.jsonl", limit=200):
        if not isinstance(row, dict):
            continue
        rows.append(
            _to_control_plane_row(
                surface="model_routing",
                target=str(row.get("selected_model") or ""),
                action=str(row.get("reason") or ""),
                actor="",
                detail=str(row.get("intent_name") or row.get("channel") or ""),
                trace_id=str(row.get("trace_id") or ""),
                at_ms=int(row.get("at_ms") or 0),
                pending_apply=False,
            )
        )
    for row in _build_agent_execution_activity_rows(
        _read_jsonl(data_dir / "dashboard" / "agent_execution.log.jsonl", limit=200)
    ):
        if not isinstance(row, dict):
            continue
        rows.append(row)

    rows = sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    filtered = [
        row
        for row in rows
        if (not surface_filter or str(row.get("surface") or "").strip().lower() == surface_filter)
        and (not actor_filter or str(row.get("actor") or "").strip().lower() == actor_filter)
        and (not target_filter or str(row.get("target") or "").strip().lower() == target_filter)
        and (not pending_only or bool(row.get("pending_apply")))
    ]
    page = filtered[safe_offset : safe_offset + safe_limit]
    pending_rows = [row for row in filtered if bool(row.get("pending_apply"))]
    ops = snapshot.get("ops") if isinstance(snapshot.get("ops"), dict) else {}
    report = _build_ops_summary_report(snapshot)
    report.update(
        {
            "ops": ops,
            "filters": {
                "surface": surface_filter,
                "actor": actor_filter,
                "target": target_filter,
                "pending_only": bool(pending_only),
            },
            "recent_activity": page,
            "pending_apply": pending_rows[:safe_limit],
            "pending_apply_total": len(pending_rows),
            "returned": len(page),
            "total": len(filtered),
            "limit": safe_limit,
            "offset": safe_offset,
        }
    )
    return report


def _latest_apply_at_ms(*, apply_rows: list[dict[str, Any]], target: str = "") -> int:
    normalized_target = str(target or "").strip().lower()
    latest_global = 0
    latest_target = 0
    for row in apply_rows:
        if not isinstance(row, dict):
            continue
        at_ms = int(row.get("at_ms") or 0)
        row_target = str(row.get("target") or "").strip().lower()
        if not row_target:
            latest_global = max(latest_global, at_ms)
        if normalized_target and row_target == normalized_target:
            latest_target = max(latest_target, at_ms)
    return max(latest_global, latest_target)


def _collect_pending_apply_state(
    *,
    data_dir: Path,
    actions_path: Path,
    apply_path: Path,
    target_field: str,
    limit: int = 50,
) -> dict[str, Any]:
    recent_actions = sorted(
        [row for row in _read_jsonl(actions_path, limit=limit) if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    apply_rows = sorted(
        [row for row in _read_jsonl(apply_path, limit=limit) if isinstance(row, dict)],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:10]
    pending = [
        row
        for row in recent_actions
        if int(row.get("at_ms") or 0)
        > _latest_apply_at_ms(
            apply_rows=apply_rows,
            target=str(row.get(target_field) or ""),
        )
    ]
    return {
        "recent_actions": recent_actions,
        "apply_rows": apply_rows,
        "pending": pending,
        "pending_count": len(pending),
        "last_apply": apply_rows[0] if apply_rows else None,
    }


def _control_plane_execution_metadata(*, surface: str, target: str) -> dict[str, Any]:
    normalized_surface = str(surface or "").strip().lower()
    normalized_target = str(target or "").strip().lower()
    if normalized_surface == "agent":
        return {
            "execution_mode": "audit_only",
            "capability_status": "audit_only",
            "runtime_actions": [],
        }
    if normalized_surface == "channel":
        from zen_claw.config.loader import load_config

        runtime_rows = {
            str(row.get("channel_name") or "").strip().lower(): row
            for row in _build_channel_runtime_control_rows(load_config())
        }
        runtime_row = runtime_rows.get(normalized_target) or {}
        runtime_actions = list(runtime_row.get("runtime_actions") or [])
        if bool(runtime_row.get("apply_supported")):
            return {
                "execution_mode": "runtime_apply_and_audit",
                "capability_status": "supported",
                "runtime_actions": runtime_actions,
            }
        return {
            "execution_mode": "audit_only",
            "capability_status": "runtime_control_unsupported",
            "runtime_actions": runtime_actions,
        }
    return {
        "execution_mode": "unsupported",
        "capability_status": "unsupported",
        "runtime_actions": [],
    }


def _update_surface_summary(
    *,
    surface_summary: dict[str, dict[str, Any]],
    row_surface: str,
    row_target: str,
    execution_meta: dict[str, Any],
) -> None:
    summary = surface_summary.setdefault(
        row_surface or "unknown",
        {
            "surface": row_surface or "unknown",
            "target_count": 0,
            "targets": [],
            "runtime_apply_target_count": 0,
            "audit_only_target_count": 0,
            "unsupported_target_count": 0,
        },
    )
    summary["target_count"] = int(summary["target_count"]) + 1
    targets = list(summary["targets"])
    targets.append(row_target)
    summary["targets"] = targets[:10]
    execution_mode = str(execution_meta.get("execution_mode") or "").strip().lower()
    capability_status = str(execution_meta.get("capability_status") or "").strip().lower()
    if execution_mode == "runtime_apply_and_audit":
        summary["runtime_apply_target_count"] = int(summary["runtime_apply_target_count"]) + 1
    if execution_mode == "audit_only":
        summary["audit_only_target_count"] = int(summary["audit_only_target_count"]) + 1
    if capability_status in {"unsupported", "runtime_control_unsupported"}:
        summary["unsupported_target_count"] = int(summary["unsupported_target_count"]) + 1


def _annotate_control_plane_execution(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    execution_meta = _control_plane_execution_metadata(
        surface=str(row.get("surface") or ""),
        target=str(row.get("target") or ""),
    )
    detail = str(row.get("detail") or "").strip()
    execution_mode = str(execution_meta.get("execution_mode") or "").strip()
    capability_status = str(execution_meta.get("capability_status") or "").strip()
    detail_suffix = f"mode={execution_mode or '-'}; capability={capability_status or '-'}"
    return {
        **row,
        **execution_meta,
        "detail": f"{detail}; {detail_suffix}" if detail else detail_suffix,
    }


async def _apply_pending_control_plane_actions(
    *,
    data_dir: Path,
    actor: str,
    pending_rows: list[dict[str, Any]] | None = None,
    surface: str = "",
    actor_filter: str = "",
    target: str = "",
) -> dict[str, Any]:
    normalized_surface = str(surface or "").strip().lower()
    normalized_actor_filter = str(actor_filter or "").strip().lower()
    normalized_target = str(target or "").strip()
    applied_rows: list[dict[str, Any]] = []
    matched_rows = [row for row in (pending_rows or []) if isinstance(row, dict)]
    seen: set[tuple[str, str]] = set()
    execution_steps: list[dict[str, Any]] = []
    surface_summary: dict[str, dict[str, Any]] = {}
    for row in matched_rows:
        row_surface = str(row.get("surface") or "").strip().lower()
        row_target = str(row.get("target") or "").strip()
        row_action = str(row.get("action") or "").strip()
        row_actor = str(row.get("actor") or "").strip()
        execution_meta = _control_plane_execution_metadata(surface=row_surface, target=row_target)
        dedupe_key = (row_surface, row_target.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if row_surface == "agent":
            event = _append_agent_apply_audit(
                data_dir=data_dir,
                actor=actor,
                note="operator_confirmed_reload_via_ops",
                target=row_target,
            )
            step_result = "audit_recorded"
        elif row_surface == "channel":
            runtime_execution = await _execute_channel_runtime_apply(row_target)
            if str(runtime_execution.get("status") or "").strip().lower() == "failed":
                execution_steps.append(
                    {
                        "step": len(execution_steps) + 1,
                        "surface": row_surface,
                        "target": row_target,
                        "action": row_action,
                        "actor": row_actor,
                        "execution_mode": str(execution_meta.get("execution_mode") or ""),
                        "capability_status": str(execution_meta.get("capability_status") or ""),
                        "runtime_actions": list(execution_meta.get("runtime_actions") or []),
                        "status": "failed",
                        "result": str(runtime_execution.get("error") or "runtime_apply_failed"),
                    }
                )
                continue
            event = _append_channel_apply_audit(
                data_dir=data_dir,
                actor=actor,
                note="operator_confirmed_reload_via_ops",
                target=row_target,
            )
            if bool(runtime_execution.get("executed")):
                step_result = "runtime_restarted_and_audit_recorded"
            else:
                step_result = f"audit_recorded_{str(runtime_execution.get('reason') or 'no_runtime_execution').strip().lower()}"
        else:
            continue
        execution_steps.append(
            {
                "step": len(execution_steps) + 1,
                "surface": row_surface,
                "target": row_target,
                "action": row_action,
                "actor": row_actor,
                "execution_mode": str(execution_meta.get("execution_mode") or ""),
                "capability_status": str(execution_meta.get("capability_status") or ""),
                "runtime_actions": list(execution_meta.get("runtime_actions") or []),
                "status": "applied",
                "result": step_result,
            }
        )
        _update_surface_summary(
            surface_summary=surface_summary,
            row_surface=row_surface,
            row_target=row_target,
            execution_meta=execution_meta,
        )
        applied_rows.append(
            {
                "surface": row_surface,
                "applied": True,
                "event": event,
            }
        )
    return {
        "ok": True,
        "surface": normalized_surface or "all",
        "actor_filter": normalized_actor_filter,
        "target": normalized_target,
        "matched_pending_total": len(matched_rows),
        "applied": len(applied_rows) > 0,
        "applied_count": len(applied_rows),
        "surface_summary": sorted(
            surface_summary.values(),
            key=lambda row: (str(row.get("surface") or ""), -int(row.get("target_count") or 0)),
        ),
        "steps": execution_steps,
        "rows": applied_rows,
    }


def _build_pending_apply_plan(
    *,
    pending_rows: list[dict[str, Any]] | None = None,
    surface: str = "",
    actor_filter: str = "",
    target: str = "",
) -> dict[str, Any]:
    normalized_surface = str(surface or "").strip().lower()
    normalized_actor_filter = str(actor_filter or "").strip().lower()
    normalized_target = str(target or "").strip()
    matched_rows = [row for row in (pending_rows or []) if isinstance(row, dict)]
    deduped_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    surface_summary: dict[str, dict[str, Any]] = {}
    for row in matched_rows:
        row_surface = str(row.get("surface") or "").strip().lower()
        row_target = str(row.get("target") or "").strip()
        execution_meta = _control_plane_execution_metadata(surface=row_surface, target=row_target)
        dedupe_key = (row_surface, row_target.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped_rows.append({**row, **execution_meta})
        _update_surface_summary(
            surface_summary=surface_summary,
            row_surface=row_surface,
            row_target=row_target,
            execution_meta=execution_meta,
        )
    return {
        "ok": True,
        "surface": normalized_surface or "all",
        "actor_filter": normalized_actor_filter,
        "target": normalized_target,
        "matched_pending_total": len(matched_rows),
        "apply_target_total": len(deduped_rows),
        "targets": [str(row.get("target") or "") for row in deduped_rows],
        "surface_summary": sorted(
            surface_summary.values(),
            key=lambda row: (str(row.get("surface") or ""), -int(row.get("target_count") or 0)),
        ),
        "steps": [
            {
                "step": idx,
                "surface": str(row.get("surface") or ""),
                "target": str(row.get("target") or ""),
                "action": str(row.get("action") or ""),
                "actor": str(row.get("actor") or ""),
                "execution_mode": str(row.get("execution_mode") or ""),
                "capability_status": str(row.get("capability_status") or ""),
                "runtime_actions": list(row.get("runtime_actions") or []),
            }
            for idx, row in enumerate(deduped_rows, start=1)
        ],
        "rows": deduped_rows,
    }


def _api_keys_file() -> Path:
    from zen_claw.config.loader import get_data_dir

    return get_data_dir() / "api_keys.json"


def _load_api_keys() -> dict[str, dict]:
    path = _api_keys_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_api_keys(keys: dict[str, dict]) -> None:
    path = _api_keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    raw = "nc-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return raw, raw[:12]


def store_api_key(raw: str) -> str:
    keys = _load_api_keys()
    hashed = _hash_key(raw)
    prefix = raw[:12]
    keys[hashed] = {"prefix": prefix, "created_at": int(time.time()), "enabled": True}
    _save_api_keys(keys)
    return prefix


def verify_api_key(raw: str) -> bool:
    hashed = _hash_key(raw)
    env_keys = str(os.environ.get("zen_claw_API_KEYS", "")).strip()
    if env_keys:
        for key in env_keys.split(","):
            token = key.strip()
            if token and _hash_key(token) == hashed:
                return True
    entry = _load_api_keys().get(hashed)
    return bool(entry and entry.get("enabled", True))


def revoke_api_key_by_prefix(prefix: str) -> bool:
    keys = _load_api_keys()
    found = False
    for _, row in keys.items():
        p = str(row.get("prefix", ""))
        if p.startswith(prefix):
            row["enabled"] = False
            found = True
    if found:
        _save_api_keys(keys)
    return found


async def _invoke_agent_text(message: str, session_id: str) -> str:
    # Graceful fallback keeps API usable in unconfigured/dev environments.
    try:
        from zen_claw.agent.loop import AgentLoop
        from zen_claw.bus.queue import MessageBus
        from zen_claw.config.loader import load_config
        from zen_claw.providers.litellm_provider import LiteLLMProvider

        cfg = load_config()
        provider_cfg = cfg.get_provider(cfg.agents.defaults.model)
        if not provider_cfg or not provider_cfg.api_key:
            return f"[echo] {message}"
        provider = LiteLLMProvider(
            api_key=provider_cfg.api_key,
            api_base=cfg.get_api_base(cfg.agents.defaults.model),
            default_model=cfg.agents.defaults.model,
            extra_headers=provider_cfg.extra_headers,
            rate_limit_delay_sec=provider_cfg.rate_limit_delay_sec,
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=cfg.workspace_path,
            model=cfg.agents.defaults.model,
            max_iterations=cfg.agents.defaults.max_tool_iterations,
            memory_recall_mode=cfg.agents.defaults.memory_recall_mode,
            enable_planning=cfg.agents.defaults.enable_planning,
            max_reflections=cfg.agents.defaults.max_reflections,
            auto_parameter_rewrite=cfg.agents.defaults.auto_parameter_rewrite,
            max_context_tokens=cfg.agents.defaults.max_tokens,
            compression_trigger_ratio=cfg.agents.defaults.compression_trigger_ratio,
            compression_hysteresis_ratio=cfg.agents.defaults.compression_hysteresis_ratio,
            compression_cooldown_turns=cfg.agents.defaults.compression_cooldown_turns,
            vision_model=cfg.agents.defaults.vision_model or None,
            thinking_model=cfg.agents.defaults.thinking_model or None,
            fallback_model=cfg.agents.defaults.fallback_model or None,
            intent_model_overrides=cfg.agents.defaults.intent_model_overrides,
            skill_permissions_mode=cfg.agents.defaults.skill_permissions_mode,
            allowed_models=cfg.agents.defaults.allowed_models,
        )
        return await loop.process_direct(
            content=message, session_key=f"web:{session_id}", channel="web_chat", chat_id=session_id
        )
    except Exception:
        return f"[echo] {message}"


class _WebChatRuntime:
    """Lazy in-process webchat runtime backed by message bus + agent loop."""

    def __init__(self, cfg: "Config"):
        from zen_claw.agent.loop import AgentLoop
        from zen_claw.bus.queue import MessageBus
        from zen_claw.channels.webchat import WebChatChannel
        from zen_claw.providers.litellm_provider import LiteLLMProvider

        provider_cfg = cfg.get_provider(cfg.agents.defaults.model)
        if not provider_cfg or not provider_cfg.api_key:
            raise RuntimeError("provider_not_configured")
        self.cfg = cfg
        self.bus = MessageBus()
        self.channel = WebChatChannel(
            cfg.channels.webchat, self.bus, media_root=cfg.workspace_path / "media"
        )
        self.channel.access_checker = lambda *_args, **_kwargs: True
        self.provider = LiteLLMProvider(
            api_key=provider_cfg.api_key,
            api_base=cfg.get_api_base(cfg.agents.defaults.model),
            default_model=cfg.agents.defaults.model,
            extra_headers=provider_cfg.extra_headers,
            rate_limit_delay_sec=provider_cfg.rate_limit_delay_sec,
        )
        self.agent = AgentLoop(
            bus=self.bus,
            provider=self.provider,
            workspace=cfg.workspace_path,
            model=cfg.agents.defaults.model,
            max_iterations=cfg.agents.defaults.max_tool_iterations,
            memory_recall_mode=cfg.agents.defaults.memory_recall_mode,
            enable_planning=cfg.agents.defaults.enable_planning,
            max_reflections=cfg.agents.defaults.max_reflections,
            auto_parameter_rewrite=cfg.agents.defaults.auto_parameter_rewrite,
            max_context_tokens=cfg.agents.defaults.max_tokens,
            compression_trigger_ratio=cfg.agents.defaults.compression_trigger_ratio,
            compression_hysteresis_ratio=cfg.agents.defaults.compression_hysteresis_ratio,
            compression_cooldown_turns=cfg.agents.defaults.compression_cooldown_turns,
            vision_model=cfg.agents.defaults.vision_model or None,
            thinking_model=cfg.agents.defaults.thinking_model or None,
            fallback_model=cfg.agents.defaults.fallback_model or None,
            intent_model_overrides=cfg.agents.defaults.intent_model_overrides,
            skill_permissions_mode=cfg.agents.defaults.skill_permissions_mode,
            allowed_models=cfg.agents.defaults.allowed_models,
        )
        self._dispatcher_task: asyncio.Task | None = None
        self._agent_task: asyncio.Task | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.channel.start()
        self._dispatcher_task = asyncio.create_task(self._dispatch_outbound())
        self._agent_task = asyncio.create_task(self.agent.run())

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        tasks = [task for task in [self._dispatcher_task, self._agent_task] if task is not None]
        self._dispatcher_task = None
        self._agent_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            await self.channel.stop()
        except Exception:
            pass

    async def _dispatch_outbound(self) -> None:
        while True:
            try:
                msg = await self.bus.consume_outbound()
                if msg.channel == "webchat":
                    await self.channel.send(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                continue


_WEBCHAT_RUNTIME_BY_LOOP: dict[int, _WebChatRuntime] = {}
_WEBCHAT_RUNTIME_LOCK: asyncio.Lock | None = None


def _channel_runtime_action_handlers() -> dict[str, dict[str, Any]]:
    return {
        "webchat": {
            "start": _start_webchat_runtime,
            "stop": _stop_webchat_runtime,
            "restart": _restart_webchat_runtime,
            "apply": _restart_webchat_runtime,
        }
    }


def _build_channel_runtime_control_rows(cfg: "Config") -> list[dict[str, Any]]:
    runtime_summaries = {"webchat": _webchat_runtime_summary()}
    rows: list[dict[str, Any]] = []
    for spec in iter_channel_specs():
        base = build_channel_rbac_row(cfg, spec)
        actions = list(base.get("runtime_actions") or [])
        summary = runtime_summaries.get(spec.key, {})
        if bool(base.get("runtime_supported")) and isinstance(summary, dict):
            rows.append(
                {
                    "channel_name": spec.key,
                    "display_name": str(base.get("display_name") or spec.display_name),
                    "supported": True,
                    "state": str(summary.get("state") or "stopped"),
                    "running_instances": int(summary.get("running_instances") or 0),
                    "known_loops": int(summary.get("known_loops") or 0),
                    "restart_supported": "restart" in actions,
                    "apply_supported": "apply" in actions,
                    "runtime_actions": actions,
                    "enabled": bool(base.get("enabled")),
                }
            )
        else:
            rows.append(
                {
                    "channel_name": spec.key,
                    "display_name": str(base.get("display_name") or spec.display_name),
                    "supported": False,
                    "state": "unsupported",
                    "running_instances": 0,
                    "known_loops": 0,
                    "restart_supported": False,
                    "apply_supported": False,
                    "runtime_actions": actions,
                    "enabled": bool(base.get("enabled")),
                }
            )
    return rows


async def _get_webchat_runtime() -> _WebChatRuntime | None:
    from zen_claw.config.loader import load_config

    global _WEBCHAT_RUNTIME_LOCK
    if _WEBCHAT_RUNTIME_LOCK is None:
        _WEBCHAT_RUNTIME_LOCK = asyncio.Lock()
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    if loop_id in _WEBCHAT_RUNTIME_BY_LOOP:
        return _WEBCHAT_RUNTIME_BY_LOOP[loop_id]
    async with _WEBCHAT_RUNTIME_LOCK:
        if loop_id in _WEBCHAT_RUNTIME_BY_LOOP:
            return _WEBCHAT_RUNTIME_BY_LOOP[loop_id]
        try:
            runtime = _WebChatRuntime(load_config())
            await runtime.start()
            _WEBCHAT_RUNTIME_BY_LOOP[loop_id] = runtime
            return runtime
        except Exception:
            return None


def _webchat_runtime_summary() -> dict[str, Any]:
    runtimes = list(_WEBCHAT_RUNTIME_BY_LOOP.values())
    running = [runtime for runtime in runtimes if getattr(runtime, "_started", False)]
    return {
        "channel_name": "webchat",
        "supported": True,
        "state": "running" if running else "stopped",
        "running_instances": len(running),
        "known_loops": len(runtimes),
    }


async def _execute_channel_runtime_apply(channel_name: str) -> dict[str, Any]:
    from zen_claw.config.loader import load_config

    normalized_name = str(channel_name or "").strip().lower()
    capability_rows = {
        str(row.get("channel_name") or "").strip().lower(): row
        for row in _build_channel_runtime_control_rows(load_config())
    }
    before = capability_rows.get(normalized_name) or {}
    if not bool(before.get("apply_supported")):
        return {
            "channel_name": normalized_name or "unknown",
            "supported": bool(before.get("supported")),
            "executed": False,
            "status": "skipped",
            "reason": "runtime_control_unsupported",
        }
    before_state = str(before.get("state") or "").strip().lower() or "unknown"
    running_instances = int(before.get("running_instances") or 0)
    if not bool(before.get("supported")):
        return {
            "channel_name": normalized_name or "unknown",
            "supported": False,
            "executed": False,
            "status": "skipped",
            "reason": "runtime_control_unsupported",
            "before_state": before_state,
            "after_state": before_state,
        }
    if before_state != "running" and running_instances <= 0:
        return {
            "channel_name": normalized_name or "unknown",
            "supported": True,
            "executed": False,
            "status": "skipped",
            "reason": "runtime_not_running",
            "before_state": before_state,
            "after_state": before_state,
        }
    try:
        apply_handler = _channel_runtime_action_handlers().get(normalized_name, {}).get("apply")
        if apply_handler is None:
            raise RuntimeError("runtime_control_unsupported")
        restart_summary = await apply_handler()
    except Exception as exc:
        return {
            "channel_name": normalized_name or "unknown",
            "supported": True,
            "executed": False,
            "status": "failed",
            "reason": "runtime_restart_failed",
            "error": str(exc),
            "before_state": before_state,
        }
    return {
        "channel_name": normalized_name or "unknown",
        "supported": True,
        "executed": True,
        "status": "restarted",
        "reason": "runtime_restarted",
        "before_state": before_state,
        "after_state": str(restart_summary.get("state") or "").strip().lower() or "unknown",
        "restart": restart_summary,
    }


async def _start_webchat_runtime() -> dict[str, Any]:
    runtime = await _get_webchat_runtime()
    if runtime is None:
        raise HTTPException(status_code=503, detail="webchat runtime unavailable")
    return _webchat_runtime_summary()


async def _restart_webchat_runtime() -> dict[str, Any]:
    await _stop_webchat_runtime()
    return await _start_webchat_runtime()


async def _stop_webchat_runtime() -> dict[str, Any]:
    stopped = 0
    for runtime in list(_WEBCHAT_RUNTIME_BY_LOOP.values()):
        if getattr(runtime, "_started", False):
            await runtime.stop()
            stopped += 1
    summary = _webchat_runtime_summary()
    summary["stopped_instances"] = stopped
    return summary


if _HAS_FASTAPI:
    import collections

    from pydantic import BaseModel, Field

    # ── Pydantic schemas ──────────────────────────────────────────────────────

    class InvokeRequest(BaseModel):
        message: str = Field(
            ...,
            description="The user message to send to the agent.",
            json_schema_extra={"example": "帮我查一下今天的天气"},
        )
        session_id: str | None = Field(
            None,
            description="Optional session ID for conversation continuity. Auto-generated if omitted.",
            json_schema_extra={"example": "abc-123"},
        )

    class InvokeResponse(BaseModel):
        response: str = Field(..., description="The agent's reply.")
        session_id: str = Field(..., description="Session ID used for this turn.")

    class RAGIngestRequest(BaseModel):
        source: str = Field(..., json_schema_extra={"example": "./docs"})
        notebook_id: str | None = Field(None, json_schema_extra={"example": "finance_kb"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "chroma"})
        metadata: dict[str, str | int | float | bool] | None = Field(default=None)

    class RAGSearchRequest(BaseModel):
        query: str = Field(..., json_schema_extra={"example": "理财产品推广"})
        notebook_id: str | None = Field(None, json_schema_extra={"example": "finance_kb"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "chroma"})
        top_k: int = Field(5, ge=1, le=20)
        source_contains: str | None = Field(None, json_schema_extra={"example": "policy"})
        filters: dict[str, str | int | float | bool] | None = Field(default=None)
        rerank: bool = Field(False, json_schema_extra={"description": "LLM 精排，需配置 provider"})
        rerank_top_n: int = Field(20, ge=1, le=50)

    class RAGRetentionRequest(BaseModel):
        notebook_id: str | None = Field(None, json_schema_extra={"example": "finance_kb"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "chroma"})
        max_documents: int = Field(0, ge=0)
        max_age_days: int = Field(0, ge=0)

    class RAGTenantPolicyRequest(BaseModel):
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "memory"})

    class RAGNotebookPolicyRequest(BaseModel):
        notebook_id: str = Field(..., json_schema_extra={"example": "finance_kb"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "memory"})
        retention_max_documents: int | None = Field(None, ge=0)
        retention_max_age_days: int | None = Field(None, ge=0)

    class RagNotebookCreateRequest(BaseModel):
        notebook_id: str = Field(..., json_schema_extra={"example": "finance_kb"})
        display_name: str = Field("", json_schema_extra={"example": "Finance KB"})
        store_backend: str = Field("", json_schema_extra={"example": "chroma"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})

    class CrawlerSourceRequest(BaseModel):
        name: str = Field(..., json_schema_extra={"example": "news"})
        url: str = Field(..., json_schema_extra={"example": "https://example.com/news"})
        notebook_id: str | None = Field(None, json_schema_extra={"example": "crawler_kb"})
        selector: str | None = Field(None, json_schema_extra={"example": ".article"})
        use_browser: bool = Field(False)
        max_chars: int = Field(20_000, ge=100)
        metadata: dict[str, str | int | float | bool] | None = Field(default=None)
        selector_type: str = Field("css", json_schema_extra={"example": "css"})
        pagination_selector: str = Field("", json_schema_extra={"example": "a.next-page"})
        max_pages: int = Field(1, ge=1, le=20)

    class CrawlerRunRequest(BaseModel):
        source_name: str = Field(..., json_schema_extra={"example": "news"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})
        store_backend: str | None = Field(None, json_schema_extra={"example": "memory"})
        source_file: str | None = Field(None, json_schema_extra={"example": "./crawler/sources.json"})

    class CrawlerScheduleRequest(BaseModel):
        source_name: str = Field(..., json_schema_extra={"example": "news"})
        cron: str = Field(..., json_schema_extra={"example": "0 */6 * * *"})
        tenant_id: str = Field("default")
        store_backend: str = Field("")
        enabled: bool = Field(True)

    class ContentGenRunRequest(BaseModel):
        industry: str = Field(..., json_schema_extra={"example": "finance"})
        channel: str = Field(
            ...,
            json_schema_extra={
                "example": "wechat_moments",
                "enum": ["generic", "wechat_moments", "wechat_article", "xiaohongshu", "douyin_script"],
            },
        )
        product_name: str = Field(..., json_schema_extra={"example": "稳健理财计划A"})
        audience: str = Field(..., json_schema_extra={"example": "35-50岁稳健型投资者"})
        style: str | None = Field(None, json_schema_extra={"example": "专业可信"})
        key_points: list[str] | None = Field(None, json_schema_extra={"example": ["中低风险", "年化收益参考区间3%-5%"]})
        count: int = Field(3, ge=1, le=5)
        use_rag: bool = Field(False)
        notebook_id: str | None = Field(None, json_schema_extra={"example": "finance_kb"})
        tenant_id: str | None = Field(None, json_schema_extra={"example": "default"})

    class ComplianceCheckRunRequest(BaseModel):
        text: str = Field(..., json_schema_extra={"example": "保证年化收益8%，稳赚不赔，市场最优产品。"})
        industry: str | None = Field(None, json_schema_extra={"example": "finance"})
        channel: str | None = Field(None, json_schema_extra={"example": "wechat_moments"})
        policy_context: str | None = Field(None, json_schema_extra={"example": "遵照中国广告法及银行业合规要求"})
        semantic: bool = Field(False)

    class SkillInstallRequest(BaseModel):
        source: str = Field(..., json_schema_extra={"example": "./incoming/skills/finance_writer"})
        name: str | None = Field(None, json_schema_extra={"example": "finance_writer"})
        overwrite: bool = Field(False)
        strict_manifest: bool = Field(False)
        sanitize: bool = Field(False)
        source_url: str | None = Field(None, json_schema_extra={"example": "https://example.com/skill.zip"})
        source_version: str | None = Field(None, json_schema_extra={"example": "1.2.3"})
        last_sync_checked_at: str | None = Field(
            None, json_schema_extra={"example": "2026-03-28T15:30:00Z"}
        )
        intake_status: str | None = Field(None, json_schema_extra={"example": "intake_review"})

    class SkillCleanupRequest(BaseModel):
        retention_hours: int = Field(24, ge=0, le=24 * 365)

    class SkillPackagePolicyUpdateRequest(BaseModel):
        preferred_physical_dir: str | None = Field(
            None, json_schema_extra={"example": "alpha_v1_0_0"}
        )
        preferred_version: str | None = Field(None, json_schema_extra={"example": "1.0.0"})
        retention_hours: int | None = Field(None, ge=0, le=24 * 365)
        require_export_for_restore: bool | None = Field(None)

    class SkillBatchPreflightRequest(BaseModel):
        names: list[str] = Field(default_factory=list)
        include_disabled: bool = Field(True)

    class SkillBatchStateRequest(BaseModel):
        names: list[str] = Field(default_factory=list)
        include_missing: bool = Field(True)

    class SkillBulkActionRequest(BaseModel):
        names: list[str] = Field(default_factory=list)
        action: str = Field(..., description="enable | disable | preflight")
        dry_run: bool = Field(False)

    class SkillPromoteRequest(BaseModel):
        declarative_intent: str = Field(..., json_schema_extra={"example": "finance_writer"})
        note: str | None = Field(None, json_schema_extra={"example": "ready for manual Gate 1 rule registration"})

    class AgentModelUpdateRequest(BaseModel):
        model: str = Field(..., json_schema_extra={"example": "deepseek-chat"})

    class AgentModelPolicyUpdateRequest(BaseModel):
        vision_model: str | None = Field(None, json_schema_extra={"example": "gpt-4.1-mini"})
        thinking_model: str | None = Field(None, json_schema_extra={"example": "o3-mini"})
        fallback_model: str | None = Field(None, json_schema_extra={"example": "gpt-4.1-mini"})
        cost_model: str | None = Field(None, json_schema_extra={"example": "gpt-4.1-mini"})
        stability_model: str | None = Field(None, json_schema_extra={"example": "gpt-4.1"})
        intent_model_overrides: dict[str, str] | None = Field(
            None,
            json_schema_extra={"example": {"finance": "gpt-4.1-mini"}},
        )
        task_type_model_overrides: dict[str, str] | None = Field(
            None,
            json_schema_extra={"example": {"message.send": "gpt-4.1-mini"}},
        )
        allowed_models: list[str] | None = Field(
            None,
            json_schema_extra={"example": ["deepseek-chat", "gpt-4.1-mini"]},
        )

    class OpsReloadExecuteRequest(BaseModel):
        surface: str = Field("", description="可选：限定执行的 surface，如 agent / channel.webchat")
        target: str = Field("", description="可选：限定目标，如 default / finance")
        dry_run: bool = Field(False, description="True → 只返回计划，不执行")

    class AgentProfileSaveRequest(BaseModel):
        profile: dict[str, Any] = Field(
            default_factory=dict,
            json_schema_extra={
                "example": {
                    "display_name": "Finance Writer",
                    "workspace": "./workspace-finance",
                    "model": "deepseek-chat",
                    "routing_keywords": ["finance"],
                    "skill_names": ["content_gen"],
                }
            },
        )

    class AgentRoutingUpdateRequest(BaseModel):
        routing_keywords: list[str] = Field(
            default_factory=list,
            json_schema_extra={"example": ["finance", "bank campaign"]},
        )

    class AgentRoutePreviewRequest(BaseModel):
        channel: str = Field(..., json_schema_extra={"example": "webchat"})
        chat_id: str = Field(..., json_schema_extra={"example": "chat-1"})
        user_id: str = Field(..., json_schema_extra={"example": "user-1"})
        content: str = Field("", json_schema_extra={"example": "need a bank campaign draft"})
        explicit_agent_id: str | None = Field(None, json_schema_extra={"example": "finance_writer"})

    class AgentRouteBindRequest(BaseModel):
        channel: str = Field(..., json_schema_extra={"example": "webchat"})
        chat_id: str = Field(..., json_schema_extra={"example": "chat-1"})
        user_id: str = Field(..., json_schema_extra={"example": "user-1"})
        agent_id: str = Field(..., json_schema_extra={"example": "finance_writer"})

    class AgentRouteBulkClearRequest(BaseModel):
        channel: str | None = Field(None, json_schema_extra={"example": "feishu"})
        agent_id: str | None = Field(None, json_schema_extra={"example": "finance_writer"})
        reason: str = Field("bulk_clear", json_schema_extra={"example": "decommission"})
        dry_run: bool = Field(False)

    class HealthResponse(BaseModel):
        status: str = Field(..., json_schema_extra={"example": "ok"})
        service: str = Field(..., json_schema_extra={"example": "zen-claw"})

    class InfoResponse(BaseModel):
        version: str
        model: str
        capabilities: list[str]

    # ── Middleware: API Key auth ───────────────────────────────────────────────

    _CHAT_HTML_PATH = Path(__file__).parent / "static" / "chat.html"

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        EXEMPT = {"/api/v1/health", "/chat", "/chat/upload"}

        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/v1/") and path not in self.EXEMPT:
                api_key = str(request.headers.get("X-API-Key", ""))
                if not api_key or not verify_api_key(api_key):
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": "invalid_api_key",
                            "detail": "Provide a valid API key via the X-API-Key header.",
                        },
                    )
            return await call_next(request)

    # ── Middleware: Simple sliding-window rate limiter ────────────────────────

    class RateLimitMiddleware(BaseHTTPMiddleware):
        """Token-bucket rate limiter: max N requests per window per IP (no extra deps)."""

        def __init__(self, app, max_requests: int = 60, window_sec: int = 60):
            super().__init__(app)
            self._max = max_requests
            self._window = window_sec
            # {ip: deque[timestamp]}
            self._buckets: dict[str, collections.deque] = collections.defaultdict(collections.deque)

        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith("/api/v1/agent/"):
                return await call_next(request)
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            bucket = self._buckets[ip]
            # Evict timestamps outside the window
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= self._max:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "rate_limit_exceeded",
                        "detail": f"Max {self._max} requests per {self._window}s.",
                    },
                    headers={"Retry-After": str(self._window)},
                )
            bucket.append(now)
            return await call_next(request)

    # ── FastAPI app ───────────────────────────────────────────────────────────

    _API_DESCRIPTION = """
## zen-claw REST API

轻量级本地 AI 代理引擎，支持工具调用、长期记忆、多渠道集成。

### 鉴权方式
所有 `/api/v1/` 端点（`/health` 除外）需在请求头携带 API Key：

```
X-API-Key: nc-xxxxxxxxxxxxxxxx
```

通过 CLI 生成密钥：`zen-claw api-key generate`

### 限流策略
调用类端点（`/agent/invoke*`）：每 IP 每 60 秒最多 60 次请求。
超限返回 **HTTP 429**，`Retry-After` 头指示等待秒数。
"""

    api_app = FastAPI(
        title="zen-claw API",
        version="1.0.0",
        description=_API_DESCRIPTION,
        contact={"name": "zen-claw", "url": "https://github.com/ZachCharles666/zen-claw-public"},
        license_info={"name": "MIT"},
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        openapi_tags=[
            {"name": "agent", "description": "向 AI 代理发送消息并获取回复"},
            {"name": "system", "description": "服务健康检查与版本信息"},
            {"name": "chat", "description": "浏览器端聊天界面及文件上传"},
        ],
    )
    api_app.add_middleware(RateLimitMiddleware, max_requests=60, window_sec=60)
    api_app.add_middleware(ApiKeyMiddleware)
    api_app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    try:
        from zen_claw.auth.middleware import MultiTenantAuthMiddleware
        from zen_claw.auth.session import SessionManager
        from zen_claw.config.loader import load_config

        _mt_config = load_config().multitenant
        if _mt_config.enabled and _mt_config.jwt_secret.get_secret_value():
            _session_mgr = SessionManager(
                secret=_mt_config.jwt_secret.get_secret_value(),
                algorithm=_mt_config.jwt_algorithm,
                expire_seconds=_mt_config.jwt_expire_seconds,
            )
            api_app.add_middleware(
                MultiTenantAuthMiddleware,
                session_manager=_session_mgr,
                public_paths=_mt_config.public_paths,
                login_path=_mt_config.login_path,
                cookie_name=_mt_config.session_cookie_name,
            )
    except Exception:
        pass
    try:
        from zen_claw.dashboard.webhooks import webhook_router

        if webhook_router is not None:
            api_app.include_router(webhook_router)
    except Exception:
        pass

    @api_app.get("/chat", response_class=HTMLResponse)
    async def chat_ui():
        if _CHAT_HTML_PATH.exists():
            return HTMLResponse(_CHAT_HTML_PATH.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="chat ui not found")

    _LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Login</title>
<style>
body{font-family:'Segoe UI','PingFang SC',sans-serif;background:#f3f6fb;margin:0;display:grid;place-items:center;min-height:100vh}
.box{background:#fff;border:1px solid #d9e1ec;border-radius:12px;padding:20px;min-width:320px}
input{width:100%;margin-top:8px;padding:8px;border:1px solid #c9d6e7;border-radius:8px}
button{margin-top:12px;width:100%;padding:10px;border:none;border-radius:8px;background:#0d6efd;color:#fff}
.err{color:#b42318;margin-top:8px}
</style></head>
<body><div class="box"><h3>zen-claw 登录</h3>{error_html}
<form method="post" action="/login"><input type="hidden" name="next" value="{next_url}">
<input name="username" placeholder="用户名"><input type="password" name="password" placeholder="密码">
<button type="submit">登录</button></form></div></body></html>"""

    @api_app.get("/login", response_class=HTMLResponse)
    async def login_page(next: str = "/", error: str = ""):
        err = f'<div class="err">{error}</div>' if error else ""
        return HTMLResponse(_LOGIN_HTML.format(error_html=err, next_url=next))

    @api_app.post("/login")
    async def login_submit(request: Request):
        from fastapi.responses import RedirectResponse

        from zen_claw.auth.session import SessionManager
        from zen_claw.auth.user import UserStore
        from zen_claw.config.loader import get_data_dir, load_config

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_url = str(form.get("next", "/"))

        cfg = load_config()
        user_store = UserStore(get_data_dir())
        user = user_store.authenticate(username=username, password=password)
        if not user:
            return HTMLResponse(
                _LOGIN_HTML.format(
                    error_html='<div class="err">用户名或密码错误</div>', next_url=next_url
                ),
                status_code=401,
            )

        session_mgr = SessionManager(
            secret=cfg.multitenant.jwt_secret.get_secret_value(),
            algorithm=cfg.multitenant.jwt_algorithm,
            expire_seconds=cfg.multitenant.jwt_expire_seconds,
        )
        token = session_mgr.create_session(user.user_id, user.tenant_id, user.username, user.role)
        response = RedirectResponse(
            url=next_url if next_url.startswith("/") else "/", status_code=302
        )
        response.set_cookie(
            key=cfg.multitenant.session_cookie_name,
            value=token,
            httponly=True,
            secure=cfg.multitenant.session_cookie_secure,
            samesite="lax",
            max_age=cfg.multitenant.jwt_expire_seconds,
        )
        return response

    @api_app.get("/logout")
    async def logout():
        from fastapi.responses import RedirectResponse

        from zen_claw.config.loader import load_config

        cfg = load_config()
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key=cfg.multitenant.session_cookie_name)
        return response

    @api_app.websocket("/chat/ws/{session_id}")
    async def chat_ws(websocket: WebSocket, session_id: str):
        runtime = await _get_webchat_runtime()
        if runtime is not None:
            required_token = str(runtime.cfg.channels.webchat.token or "").strip()
            if required_token:
                provided = str(websocket.query_params.get("token") or "").strip()
                if provided != required_token:
                    await websocket.close(code=1008, reason="unauthorized")
                    return
        await websocket.accept()
        try:
            while True:
                payload = json.loads(await websocket.receive_text())
                if payload.get("type") != "message":
                    continue
                content = str(payload.get("content", "")).strip()
                if not content:
                    continue
                if runtime is not None:
                    sender_id = str(payload.get("sender_id") or "webchat_user")
                    media = payload.get("media")
                    media_list = media if isinstance(media, list) else None
                    await runtime.channel.ingest_user_message(
                        session_id=session_id,
                        sender_id=sender_id,
                        content=content,
                        media=media_list,
                        metadata={"session_key": f"webchat:{session_id}"},
                    )
                    out = await runtime.channel.pop_response(session_id, timeout_sec=120.0)
                    if out is None:
                        raise RuntimeError("webchat_response_timeout")
                    answer = out.content
                else:
                    answer = await _invoke_agent_text(content, session_id)
                for ch in answer:
                    await websocket.send_text(json.dumps({"type": "token", "content": ch}))
                await websocket.send_text(json.dumps({"type": "done"}))
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))

    @api_app.post("/chat/upload")
    async def chat_upload(request: Request):
        from zen_claw.config.loader import get_data_dir

        upload_dir = get_data_dir() / "uploads" / "chat"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        saved_paths: list[str] = []
        try:
            form = await request.form()
            files = form.getlist("files")
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "saved": [],
                    "media_paths": [],
                    "rag_status": "upload_parse_failed",
                    "message": "invalid upload payload",
                },
            )
        for f in files:
            filename = getattr(f, "filename", None)
            reader = getattr(f, "read", None)
            if not filename or not callable(reader):
                continue
            name = Path(str(filename)).name
            stamp = uuid.uuid4().hex[:8]
            dest = upload_dir / f"{stamp}_{name}"
            dest.write_bytes(await reader())
            saved.append(dest.name)
            saved_paths.append(str(dest))
        rag_status = "rag_not_installed"
        try:
            from zen_claw.agent.tools.knowledge import KnowledgeAddTool

            tool = KnowledgeAddTool(data_dir=get_data_dir())
            for file_path in saved_paths:
                await tool.execute(source=file_path, notebook_id="chat_uploads")
            rag_status = "ingested"
        except Exception:
            rag_status = "rag_not_available"
        return JSONResponse(
            {
                "saved": saved,
                "media_paths": saved_paths,
                "rag_status": rag_status,
                "message": f"{len(saved)} file(s) uploaded. RAG status: {rag_status}.",
            }
        )

    api_router = APIRouter(prefix="/api/v1")

    def _resolve_rag_tenant_id(request: Request, explicit_tenant_id: str = "") -> str:
        from zen_claw.auth.paths import DEFAULT_TENANT_ID

        explicit = str(explicit_tenant_id or "").strip()
        state_tenant = str(getattr(request.state, "tenant_id", "") or "").strip()
        if explicit and state_tenant and explicit != state_tenant:
            raise HTTPException(
                status_code=403,
                detail=(
                    "cross-tenant rag access is forbidden: "
                    f"authenticated tenant={state_tenant}, requested tenant={explicit}"
                ),
            )
        if explicit:
            return explicit
        return state_tenant or DEFAULT_TENANT_ID

    def _read_tenant_policy(tenant_id: str) -> dict[str, Any]:
        from zen_claw.auth.tenant import TenantStore
        from zen_claw.config.loader import get_data_dir

        tenant = TenantStore(get_data_dir()).get(tenant_id)
        return {
            "tenant_id": tenant_id,
            "store_backend": str(getattr(tenant, "store_backend", "") or ""),
            "enabled": bool(getattr(tenant, "enabled", True)) if tenant else True,
            "exists": tenant is not None,
        }

    def _read_notebook_policy(tenant_id: str, notebook_id: str) -> dict[str, Any]:
        from zen_claw.auth.paths import tenant_data_dir
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.notebook import NotebookManager

        manager = NotebookManager(tenant_data_dir(get_data_dir(), tenant_id))
        notebook = manager.get(notebook_id)
        return {
            "tenant_id": tenant_id,
            "notebook_id": str(notebook_id or "").strip(),
            "store_backend": str(getattr(notebook, "store_backend", "") or ""),
            "retention_max_documents": (
                int(getattr(notebook, "retention_max_documents", 0) or 0) if notebook else 0
            ),
            "retention_max_age_days": (
                int(getattr(notebook, "retention_max_age_days", 0) or 0) if notebook else 0
            ),
            "doc_count": int(getattr(notebook, "doc_count", 0) or 0) if notebook else 0,
            "exists": notebook is not None,
        }

    @api_router.get(
        "/health",
        summary="健康检查",
        description="返回服务运行状态，无需鉴权。可用于负载均衡器或监控系统的存活探针。",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def api_health():
        return HealthResponse(status="ok", service="zen-claw")

    @api_router.get(
        "/info",
        summary="服务信息",
        description="返回当前部署版本、默认模型以及已启用的能力列表。",
        response_model=InfoResponse,
        tags=["system"],
    )
    async def api_info():
        from zen_claw import __version__
        from zen_claw.config.loader import load_config

        cfg = load_config()
        return InfoResponse(
            version=__version__,
            model=cfg.agents.defaults.model,
            capabilities=["text", "tools", "memory"],
        )

    @api_router.get(
        "/skills/health-summary",
        summary="技能健康概览",
        description="返回所有 skill 的聚合健康指标：enabled/manifest_ok/trusted/runtime_covered/preflight 统计及百分比。",
        tags=["skills"],
    )
    async def api_skills_health_summary():
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        inventory = loader.build_skills_inventory()
        rows = list(inventory.get("skills") or [])
        recent_checks = sorted(
            [
                row
                for row in _read_jsonl(get_data_dir() / "dashboard" / "skills_checks.log.jsonl", limit=200)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )
        latest_checks: dict[str, dict[str, Any]] = {}
        for row in recent_checks:
            skill_name = str(row.get("skill_name") or "").strip()
            if skill_name and skill_name not in latest_checks:
                latest_checks[skill_name] = row
        return JSONResponse(_build_skill_health_summary(rows, latest_checks=latest_checks))

    @api_router.post(
        "/skills/bulk-action",
        summary="技能批量操作",
        description="对多个 skill 执行批量 enable / disable / preflight 操作，支持 dry_run 预览。",
        tags=["skills"],
    )
    async def api_skills_bulk_action(req: SkillBulkActionRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        names = [str(n or "").strip() for n in list(req.names or []) if str(n or "").strip()]
        action = str(req.action or "").strip().lower()
        dry_run = bool(req.dry_run)

        if action not in ("enable", "disable", "preflight"):
            raise HTTPException(status_code=400, detail=f"invalid action: {action!r}. must be enable|disable|preflight")
        if len(names) > 20:
            raise HTTPException(status_code=400, detail=f"too many names: {len(names)} > 20")
        if not names:
            raise HTTPException(status_code=400, detail="names list is empty")

        results: list[dict[str, Any]] = []
        for name in names:
            try:
                if action == "preflight":
                    row = _run_skill_preflight(loader=loader, name=name)
                    _append_skill_check_audit(data_dir=get_data_dir(), payload=row, actor="api_key_bulk")
                    results.append({"name": name, "ok": bool(row.get("ok")), "message": row.get("detail", "")})
                elif action in ("enable", "disable"):
                    enabled = action == "enable"
                    if dry_run:
                        current = loader.is_skill_enabled(name)
                        results.append({"name": name, "ok": True, "message": f"dry_run: would {'enable' if enabled else 'disable'} (currently {'enabled' if current else 'disabled'})"})
                    else:
                        ok = loader.set_skill_enabled(name, enabled)
                        results.append({"name": name, "ok": bool(ok), "message": f"{'enabled' if enabled else 'disabled'}"})
            except HTTPException as exc:
                results.append({"name": name, "ok": False, "message": str(exc.detail)})
            except Exception as exc:
                results.append({"name": name, "ok": False, "message": str(exc)})

        succeeded = sum(1 for r in results if r.get("ok"))
        failed = len(results) - succeeded
        return JSONResponse({
            "action": action,
            "dry_run": dry_run,
            "results": results,
            "succeeded": succeeded,
            "failed": failed,
            "reload_required": False,
            "apply_state": _build_apply_state(
                surface="skill",
                target=f"bulk:{action}",
                reload_required=False,
                execution_mode="preview" if dry_run else action,
            ),
        })

    @api_router.get(
        "/skills",
        summary="技能清单",
        description="返回当前 workspace/builtin 技能库存、启用状态和 manifest 就绪情况。",
        tags=["skills"],
    )
    async def api_skills():
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        recent_checks = sorted(
            [
                row
                for row in _read_jsonl(get_data_dir() / "dashboard" / "skills_checks.log.jsonl", limit=50)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )[:10]
        payload = loader.build_skills_inventory()
        latest_checks: dict[str, dict[str, Any]] = {}
        for row in recent_checks:
            skill_name = str(row.get("skill_name") or "").strip()
            if skill_name and skill_name not in latest_checks:
                latest_checks[skill_name] = row
        payload["skills"] = [
            {
                **row,
                "last_check": latest_checks.get(str(row.get("name") or "").strip()) or None,
            }
            for row in list(payload.get("skills") or [])
        ]
        payload["recent_checks"] = recent_checks[:5]
        payload["checks_total"] = len(recent_checks)
        payload["failed_checks"] = len([row for row in recent_checks if not bool(row.get("ok"))])
        recent_exports = sorted(
            [
                row
                for row in _read_jsonl(get_data_dir() / "dashboard" / "skills_exports.log.jsonl", limit=50)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )[:10]
        payload["recent_exports"] = recent_exports[:5]
        payload["exports_total"] = len(recent_exports)
        payload["journal_pending_count"] = len(
            [item for item in list(loader._load_journal() or []) if isinstance(item, dict)]
        )
        payload["versioned_dirs_count"] = len(
            [
                row
                for row in list(payload.get("skills") or [])
                if "_v" in str(row.get("physical_dir") or "")
            ]
        )
        return JSONResponse(payload)

    @api_router.post(
        "/skills/{name}/preflight",
        summary="技能预检",
        description="执行一个轻量技能预检，返回 manifest/integrity/tests 就绪摘要并记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_preflight(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        payload = _run_skill_preflight(loader=loader, name=name)
        _append_skill_check_audit(data_dir=get_data_dir(), payload=payload, actor="api_key")
        return JSONResponse(
            {
                **payload,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=str(name),
                    reload_required=False,
                    execution_mode="preflight",
                ),
            }
        )

    @api_router.post(
        "/skills/preflight-batch",
        summary="批量技能预检",
        description="对多个技能执行轻量预检，返回聚合结果并为每个技能记录检查审计事件。",
        tags=["skills"],
    )
    async def api_skills_preflight_batch(req: SkillBatchPreflightRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        payload = _run_skill_preflight_batch(
            loader=loader,
            names=list(req.names or []),
            include_disabled=bool(req.include_disabled),
        )
        for row in list(payload.get("rows") or []):
            _append_skill_check_audit(data_dir=get_data_dir(), payload=row, actor="api_key")
        return JSONResponse(
            {
                **payload,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target="batch:preflight",
                    reload_required=False,
                    execution_mode="preflight",
                ),
            }
        )

    @api_router.post(
        "/skills/install",
        summary="安装技能",
        description="从本地目录或 zip 安装一个 workspace skill，并记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_install(req: SkillInstallRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        loader = SkillsLoader(cfg.workspace_path)
        source = Path(str(req.source or "").strip()).expanduser()
        source_kind = "zip" if source.suffix.lower() == ".zip" else "dir"
        install_name = str(req.name or "").strip() or (source.stem if source_kind == "zip" else source.name)
        installer_name = (
            "install_and_sanitize_skill_from_zip"
            if req.sanitize and source_kind == "zip"
            else "install_and_sanitize_skill_from_dir"
            if req.sanitize
            else "install_skill_from_zip"
            if source_kind == "zip"
            else "install_skill_from_dir"
        )
        installer = getattr(loader, installer_name)
        ok, msg = installer(
            source,
            name=req.name,
            overwrite=bool(req.overwrite),
            require_manifest=bool(req.strict_manifest),
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        metadata_updates = {
            "source_url": str(req.source_url or "").strip(),
            "source_version": str(req.source_version or "").strip(),
            "last_sync_checked_at": str(req.last_sync_checked_at or "").strip(),
            "intake_status": str(req.intake_status or ("intake_review" if str(req.source_url or "").strip() else "local")).strip(),
        }
        metadata_updates = {key: value for key, value in metadata_updates.items() if value}
        if metadata_updates:
            loader.update_skill_governance_metadata(install_name, **metadata_updates)
        event = _append_skill_action_audit(
            data_dir=data_dir,
            skill_name=install_name,
            action="install",
            enabled=True,
            actor="api_key",
            detail=msg,
        )
        return JSONResponse(
            {
                "ok": True,
                "name": install_name,
                "source": str(source),
                "source_kind": source_kind,
                "overwrite": bool(req.overwrite),
                "strict_manifest": bool(req.strict_manifest),
                "sanitize": bool(req.sanitize),
                "governance": metadata_updates,
                "message": msg,
                "event": event,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=install_name,
                    reload_required=False,
                    execution_mode="install",
                ),
            }
        )

    @api_router.post(
        "/skills/gc-preview",
        summary="预览技能旧版本清理",
        description="预览 workspace 中会被 stale-version cleanup 删除的旧版本 skill 目录。",
        tags=["skills"],
    )
    async def api_skills_gc_preview(req: SkillCleanupRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        return JSONResponse(_preview_skill_gc(loader=loader, retention_hours=req.retention_hours))

    @api_router.get(
        "/skills/history",
        summary="技能活动历史",
        description="返回聚合后的技能 action/check/export 历史，并支持过滤与 CSV 导出。",
        tags=["skills"],
    )
    async def api_skills_history(
        skill_name: str = "",
        event_kind: str = "",
        actor: str = "",
        ok_only: str = "",
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        payload = _read_skill_activity_history(
            data_dir=get_data_dir(),
            skill_name=skill_name,
            event_kind=event_kind,
            actor=actor,
            ok_only=ok_only,
            limit=limit,
            offset=offset,
        )
        if str(format or "").strip().lower() == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                ["at_ms", "skill_name", "event_kind", "action", "ok", "actor", "detail", "trace_id"]
            )
            for row in list(payload.get("rows") or []):
                writer.writerow(
                    [
                        row.get("at_ms"),
                        row.get("skill_name"),
                        row.get("event_kind"),
                        row.get("action"),
                        row.get("ok"),
                        row.get("actor"),
                        row.get("detail"),
                        row.get("trace_id"),
                    ]
                )
            return StreamingResponse(
                iter([buffer.getvalue().encode("utf-8")]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=skill-activity-history.csv"},
            )
        return JSONResponse(payload)

    @api_router.get(
        "/skills/{name}/restore-plan",
        summary="技能恢复预览",
        description="返回当前 skill package policy 下的 restore plan、安全阻断原因和告警，不执行恢复。",
        tags=["skills"],
    )
    async def api_skill_restore_plan(name: str):
        return _read_skill_restore_plan(name=name)

    @api_router.get(
        "/skills/{name}",
        summary="技能详情",
        description="返回一个 skill 的库存行、manifest、预检结果以及最近的 action/check/export 细节。",
        tags=["skills"],
    )
    async def api_skill_detail(name: str):
        return _read_skill_detail(name=name)

    @api_router.post(
        "/skills/{name}/package-policy",
        summary="更新技能包策略",
        description="更新一个 skill 的 package policy，例如 preferred dir/version、cleanup retention 和 restore 安全要求。",
        tags=["skills"],
    )
    async def api_skill_update_package_policy(name: str, req: SkillPackagePolicyUpdateRequest):
        return _update_skill_package_policy(
            name=name,
            preferred_physical_dir=req.preferred_physical_dir,
            preferred_version=req.preferred_version,
            retention_hours=req.retention_hours,
            require_export_for_restore=req.require_export_for_restore,
        )

    @api_router.post(
        "/skills/{name}/promote",
        summary="标记技能 promote 结果",
        description="记录一个 skill 已通过人工 promote 评审，并附带目标 DeclarativeIntent 名称与审计说明。",
        tags=["skills"],
    )
    async def api_skill_promote(name: str, req: SkillPromoteRequest):
        return _promote_skill(name=name, declarative_intent=req.declarative_intent, note=req.note)

    @api_router.post(
        "/skills/{name}/export",
        summary="导出技能",
        description="将一个 skill 导出为 zip 包到 workspace 内置 exports 目录，并记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_export(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        loader = SkillsLoader(cfg.workspace_path)
        package_state = _read_skill_package_state(loader=loader, skill_name=name, data_dir=data_dir)
        selected_row = next(
            (
                row
                for row in list(package_state.get("package_rows") or [])
                if isinstance(row, dict) and bool(row.get("selected"))
            ),
            {},
        )
        out_dir = cfg.workspace_path / ".zen-claw" / "exports"
        out_path = out_dir / f"{str(name or '').strip()}.zip"
        ok, msg, export_hash = loader.export_skill_to_zip(name, out_path, overwrite=True)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        payload = _append_skill_export_audit(
            data_dir=data_dir,
            skill_name=name,
            out_path=out_path,
            message=msg,
            actor="api_key",
            exported_physical_dir=str(selected_row.get("physical_dir") or ""),
            exported_version=str(selected_row.get("version") or ""),
            export_hash=export_hash,
        )
        return JSONResponse(
            {
                **payload,
                "export_hash": export_hash,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=str(name),
                    reload_required=False,
                    execution_mode="export",
                ),
            }
        )

    @api_router.post(
        "/skills/{name}/restore",
        summary="按技能包策略恢复技能",
        description="基于当前 skill package policy 和最近导出包执行恢复，并返回 restore plan。保留 restore-export 作为兼容入口。",
        tags=["skills"],
    )
    async def api_skills_restore(name: str):
        return _restore_skill_with_policy(name=name)

    @api_router.post(
        "/skills/{name}/restore-export",
        summary="从最近导出恢复技能",
        description="使用最近一次导出的 zip 重新安装该 skill，形成最小 rollback-style operator flow。",
        tags=["skills"],
    )
    async def api_skills_restore_export(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        loader = SkillsLoader(cfg.workspace_path)
        export_path = _latest_skill_export_path(data_dir=data_dir, skill_name=name)
        if export_path is None:
            raise HTTPException(status_code=404, detail=f"no exported package found for skill: {name}")
        ok, msg = loader.install_skill_from_zip(
            export_path,
            name=name,
            overwrite=True,
            require_manifest=False,
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        event = _append_skill_action_audit(
            data_dir=data_dir,
            skill_name=name,
            action="restore_export",
            enabled=True,
            actor="api_key",
            detail=f"{msg} | source={export_path}",
        )
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "source": str(export_path),
                "message": msg,
                "event": event,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=str(name),
                    reload_required=False,
                    execution_mode="restore_export",
                ),
            }
        )

    @api_router.post(
        "/skills/gc-run",
        summary="执行技能旧版本清理",
        description="执行 workspace stale-version cleanup，并记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_gc_run(req: SkillCleanupRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        loader = SkillsLoader(cfg.workspace_path)
        preview = _preview_skill_gc(loader=loader, retention_hours=req.retention_hours)
        deleted = loader.gc_cleanup(retention_hours=req.retention_hours)
        event = _append_skill_action_audit(
            data_dir=data_dir,
            skill_name="__gc__",
            action="gc_cleanup",
            enabled=True,
            actor="api_key",
            detail=f"deleted={deleted}; retention_hours={int(req.retention_hours)}",
        )
        return JSONResponse(
            {
                "ok": True,
                "deleted_count": int(deleted),
                "retention_hours": int(req.retention_hours),
                "preview": preview,
                "event": event,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target="__gc__",
                    reload_required=False,
                    execution_mode="gc_cleanup",
                ),
            }
        )

    @api_router.post(
        "/skills/{name}/uninstall",
        summary="卸载技能",
        description="卸载一个 workspace skill，并记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_uninstall(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        loader = SkillsLoader(cfg.workspace_path)
        ok, msg = loader.uninstall_skill(name)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        event = _append_skill_action_audit(
            data_dir=data_dir,
            skill_name=name,
            action="uninstall",
            enabled=False,
            actor="api_key",
            detail=msg,
        )
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "enabled": False,
                "message": msg,
                "event": event,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=str(name),
                    reload_required=False,
                    execution_mode="uninstall",
                ),
            }
        )

    @api_router.get(
        "/agents",
        summary="Agent 清单",
        description="返回当前注册的 agent profile 清单及其关键运行配置摘要。",
        tags=["agent"],
    )
    async def api_agents():
        from zen_claw.agent.pool import list_agent_profiles
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        rows = [
            {
                "agent_id": row.agent_id,
                "display_name": row.display_name,
                "description": row.description,
                "model": row.model,
                "vision_model": row.vision_model,
                "thinking_model": row.thinking_model,
                "fallback_model": row.fallback_model,
                "cost_model": row.cost_model,
                "stability_model": row.stability_model,
                "planning_enabled": bool(row.enable_planning),
                "workspace": str(row.workspace),
                "registered": bool(row.is_registered),
                "intent_model_overrides": dict(row.intent_model_overrides or {}),
                "task_type_model_overrides": dict(row.task_type_model_overrides or {}),
                "allowed_models": list(row.allowed_models or []),
                "routing_keywords": list(
                    getattr(cfg.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
                ),
                "routing_keywords_count": len(
                    getattr(cfg.agents.profiles.get(row.agent_id), "routing_keywords", []) or []
                ),
                "skill_names": list(row.skill_names or []),
                "skill_count": len(row.skill_names or []),
                "allowed_tools_count": len(row.allowed_tools or []),
                "denied_tools_count": len(row.denied_tools or []),
            }
            for row in list_agent_profiles(cfg)
        ]
        pending_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "agent_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "agent_apply.log.jsonl",
            target_field="agent_id",
        )
        recent_actions = list(pending_state["recent_actions"])
        pending = list(pending_state["pending"])
        return JSONResponse(
            {
                "agents": rows,
                "total": len(rows),
                "recent_actions": recent_actions[:5],
                "actions_total": len(recent_actions),
                "pending_reload": len(pending) > 0,
                "pending_reload_count": len(pending),
                "last_apply": pending_state["last_apply"],
            }
        )

    @api_router.get(
        "/agents/history",
        summary="Agent Action History",
        description="返回 agent 配置动作历史，支持按 agent/action/actor 过滤和 CSV 导出。",
        tags=["agent"],
    )
    async def api_agents_history(
        agent_id: str = "",
        action: str = "",
        actor: str = "",
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        payload = _read_agent_action_history(
            data_dir=get_data_dir(),
            agent_id=agent_id,
            action=action,
            actor=actor,
            limit=limit,
            offset=offset,
        )
        if str(format or "").strip().lower() == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    "at_ms",
                    "agent_id",
                    "action",
                    "planning_enabled",
                    "actor",
                    "detail",
                    "trace_id",
                ]
            )
            for row in list(payload.get("rows") or []):
                writer.writerow(
                    [
                        row.get("at_ms"),
                        row.get("agent_id"),
                        row.get("action"),
                        row.get("planning_enabled"),
                        row.get("actor"),
                        row.get("detail"),
                        row.get("trace_id"),
                    ]
                )
            return StreamingResponse(
                iter([buffer.getvalue().encode("utf-8")]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=agent-action-history.csv"},
            )
        return JSONResponse(payload)

    @api_router.get(
        "/agents/routes/audit",
        summary="Agent Route 审计日志",
        description="返回跨所有 route key 的路由变更审计记录，支持按 channel/agent_id/reason 及时间范围过滤，支持 CSV 导出。",
        tags=["agent"],
    )
    async def api_agents_routes_audit_before(
        channel: str = "",
        agent_id: str = "",
        reason: str = "",
        from_at_ms: int = 0,
        to_at_ms: int = 0,
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import load_config

        cfg = load_config()
        store = _ensure_agent_route_store(cfg)
        bounded_limit = max(1, min(int(limit or 20), 100))
        bounded_offset = max(0, int(offset or 0))
        fmt = str(format or "json").strip().lower()
        audit = store.list_audit_all(
            channel=str(channel or ""),
            agent_id=str(agent_id or ""),
            reason=str(reason or ""),
            from_at_ms=max(0, int(from_at_ms or 0)),
            to_at_ms=max(0, int(to_at_ms or 0)),
            limit=bounded_limit,
            offset=bounded_offset,
        )
        if fmt == "csv":
            header = ["route_key", "channel", "chat_id", "user_id", "previous_agent_id", "new_agent_id", "reason", "at_ms"]
            lines = [",".join(header)]
            for row in audit:
                values = []
                for key in header:
                    value = str(row.get(key) or "")
                    if any(ch2 in value for ch2 in [",", '"', "\n", "\r"]):
                        value = '"' + value.replace('"', '""') + '"'
                    values.append(value)
                lines.append(",".join(values))
            csv_payload = "\n".join(lines) + "\n"
            return StreamingResponse(
                iter([csv_payload]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=agent-route-audit.csv"},
            )
        return JSONResponse(
            {
                "audit": audit,
                "total": len(audit),
                "filters": {
                    "channel": str(channel or ""),
                    "agent_id": str(agent_id or ""),
                    "reason": str(reason or ""),
                    "from_at_ms": max(0, int(from_at_ms or 0)),
                    "to_at_ms": max(0, int(to_at_ms or 0)),
                },
                "limit": bounded_limit,
                "offset": bounded_offset,
            }
        )

    @api_router.get(
        "/agents/routes",
        summary="列出所有 Sticky Route Bindings",
        description="返回当前所有活跃 sticky agent route，支持按 channel / agent_id 过滤及分页。",
        tags=["agent"],
    )
    async def api_agents_list_routes_before(
        channel: str = "",
        agent_id: str = "",
        limit: int = 20,
        offset: int = 0,
    ):
        from zen_claw.config.loader import load_config

        cfg = load_config()
        store = _ensure_agent_route_store(cfg)
        bounded_limit = max(1, min(int(limit or 20), 100))
        bounded_offset = max(0, int(offset or 0))
        routes = store.list_routes(
            channel=str(channel or ""),
            agent_id=str(agent_id or ""),
            limit=bounded_limit,
            offset=bounded_offset,
        )
        return JSONResponse(
            {
                "routes": routes,
                "total": len(routes),
                "channel_filter": str(channel or ""),
                "agent_id_filter": str(agent_id or ""),
                "limit": bounded_limit,
                "offset": bounded_offset,
            }
        )

    @api_router.delete(
        "/agents/routes",
        summary="批量清除 Sticky Route Bindings",
        description=(
            "批量清除匹配 channel/agent_id 过滤条件的 sticky route。"
            "channel 和 agent_id 至少填写一个，防止误清全表。"
            "dry_run=true 时只预览不执行。"
        ),
        tags=["agent"],
    )
    async def api_agents_bulk_clear_routes_before(req: AgentRouteBulkClearRequest):
        from zen_claw.config.loader import load_config

        ch = str(req.channel or "").strip()
        aid = str(req.agent_id or "").strip()
        if not ch and not aid:
            raise HTTPException(
                status_code=400,
                detail="At least one of channel or agent_id is required",
            )
        cfg = load_config()
        store = _ensure_agent_route_store(cfg)
        result = store.clear_routes_by(
            channel=ch,
            agent_id=aid,
            reason=str(req.reason or "bulk_clear"),
            dry_run=bool(req.dry_run),
        )
        return JSONResponse(
            {
                "ok": True,
                "cleared_count": int(result.get("cleared") or 0),
                "preview_routes": result.get("preview") or [],
                "reason": str(req.reason or "bulk_clear"),
                "dry_run": bool(req.dry_run),
                "channel_filter": ch,
                "agent_id_filter": aid,
            }
        )

    @api_router.get(
        "/agents/{agent_id}",
        summary="Agent Profile 详情",
        description="返回一个 agent profile 的完整生效配置、原始 profile overrides，以及引用它的 channel 清单。",
        tags=["agent"],
    )
    async def api_agent_detail(agent_id: str):
        return _read_agent_profile_detail(agent_id=agent_id)

    @api_router.post(
        "/agents/{agent_id}/profile",
        summary="Create or Update Agent Profile",
        description="创建或整体更新一个命名 agent profile，并返回保存前后的 profile 内容。默认 agent 不能通过该接口整体替换。",
        tags=["agent"],
    )
    async def api_agent_save_profile(agent_id: str, req: AgentProfileSaveRequest):
        return _save_agent_profile(agent_id=agent_id, profile_payload=dict(req.profile or {}))

    @api_router.delete(
        "/agents/{agent_id}",
        summary="Delete Agent Profile",
        description="删除一个命名 agent profile，并返回删除前内容与仍引用它的 channel 清单。",
        tags=["agent"],
    )
    async def api_agent_delete(agent_id: str):
        return _delete_agent_profile(agent_id=agent_id)

    @api_router.post(
        "/agents/reload-ack",
        summary="确认 Agent 配置已应用",
        description="记录一次 agent 配置变更已被 operator 应用/重载的确认事件，用于清除 pending apply 视图。",
        tags=["agent"],
    )
    async def api_agents_reload_ack():
        from zen_claw.config.loader import get_data_dir

        data_dir = get_data_dir()
        before_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "agent_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "agent_apply.log.jsonl",
            target_field="agent_id",
        )
        event = _append_agent_apply_audit(
            data_dir=data_dir,
            actor="api_key",
            note="operator_confirmed_reload",
        )
        after_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "agent_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "agent_apply.log.jsonl",
            target_field="agent_id",
        )
        return JSONResponse(
            {
                "ok": True,
                "applied": True,
                "event": event,
                "pending_before": int(before_state["pending_count"]),
                "pending_after": int(after_state["pending_count"]),
                "cleared_count": max(
                    0, int(before_state["pending_count"]) - int(after_state["pending_count"])
                ),
            }
        )

    @api_router.post(
        "/agents/{agent_id}/planning/enable",
        summary="启用 Agent Planning",
        description="以配置级方式启用一个 agent profile 的 planning；当前通常需要后续 reload 才会生效。",
        tags=["agent"],
    )
    async def api_agents_enable_planning(agent_id: str):
        return _update_agent_planning(agent_id=agent_id, enabled=True)

    @api_router.post(
        "/agents/{agent_id}/planning/disable",
        summary="停用 Agent Planning",
        description="以配置级方式停用一个 agent profile 的 planning；当前通常需要后续 reload 才会生效。",
        tags=["agent"],
    )
    async def api_agents_disable_planning(agent_id: str):
        return _update_agent_planning(agent_id=agent_id, enabled=False)

    @api_router.post(
        "/agents/{agent_id}/model",
        summary="更新 Agent Model",
        description="以配置级方式更新一个 agent profile 的 model；当前通常需要后续 reload 才会生效。",
        tags=["agent"],
    )
    async def api_agents_update_model(agent_id: str, req: AgentModelUpdateRequest):
        return _update_agent_model(agent_id=agent_id, model=req.model)

    @api_router.get(
        "/agents/{agent_id}/model-policy",
        summary="读取 Agent Model Policy",
        description="返回 agent profile 当前生效的 model policy：model / thinking_model / fallback_model / cost_model / stability_model / intent_model_overrides / task_type_model_overrides。",
        tags=["agent"],
    )
    async def api_agents_get_model_policy(agent_id: str):
        from zen_claw.agent.pool import normalize_agent_id
        from zen_claw.config.loader import load_config
        from zen_claw.config.schema import AgentProfileConfig

        cfg = load_config()
        aid = normalize_agent_id(agent_id)
        if aid == "default":
            target = cfg.agents.defaults
        else:
            profile = cfg.agents.profiles.get(aid)
            if profile is None:
                raise HTTPException(status_code=404, detail=f"agent not found: {aid}")
            if not isinstance(profile, AgentProfileConfig):
                profile = AgentProfileConfig.model_validate(profile)
            target = profile
        return JSONResponse({
            "agent_id": aid,
            "model": str(getattr(target, "model", "") or ""),
            "thinking_model": str(getattr(target, "thinking_model", "") or ""),
            "fallback_model": str(getattr(target, "fallback_model", "") or ""),
            "cost_model": str(getattr(target, "cost_model", "") or ""),
            "stability_model": str(getattr(target, "stability_model", "") or ""),
            "vision_model": str(getattr(target, "vision_model", "") or ""),
            "intent_model_overrides": dict(getattr(target, "intent_model_overrides", {}) or {}),
            "task_type_model_overrides": dict(getattr(target, "task_type_model_overrides", {}) or {}),
            "allowed_models": list(getattr(target, "allowed_models", []) or []),
        })

    @api_router.post(
        "/agents/{agent_id}/model-policy",
        summary="更新 Agent Model Policy",
        description="以配置级方式更新 agent profile 的辅助 model policy，例如 vision/thinking/fallback、intent overrides 和 allowed models；当前通常需要后续 reload 才会生效。",
        tags=["agent"],
    )
    async def api_agents_update_model_policy(agent_id: str, req: AgentModelPolicyUpdateRequest):
        return _update_agent_model_policy(
            agent_id=agent_id,
            vision_model=req.vision_model,
            thinking_model=req.thinking_model,
            fallback_model=req.fallback_model,
            cost_model=req.cost_model,
            stability_model=req.stability_model,
            intent_model_overrides=req.intent_model_overrides,
            task_type_model_overrides=req.task_type_model_overrides,
            allowed_models=req.allowed_models,
        )

    @api_router.post(
        "/agents/{agent_id}/routing-keywords",
        summary="更新 Agent Routing Keywords",
        description="以配置级方式更新一个已注册 agent profile 的 routing keywords；当前通常需要后续 reload 才会生效。",
        tags=["agent"],
    )
    async def api_agents_update_routing_keywords(agent_id: str, req: AgentRoutingUpdateRequest):
        return _update_agent_routing_keywords(
            agent_id=agent_id,
            routing_keywords=list(req.routing_keywords or []),
        )

    @api_router.post(
        "/agents/route-preview",
        summary="预览 Agent Route",
        description=(
            "用当前 agent registry/router 配置预览一条入站消息会被路由到哪个 agent，"
            "并返回 route reason、sticky binding 和 channel default 摘要。"
        ),
        tags=["agent"],
    )
    async def api_agents_route_preview(req: AgentRoutePreviewRequest):
        return _preview_agent_route(
            channel=req.channel,
            chat_id=req.chat_id,
            user_id=req.user_id,
            content=req.content,
            explicit_agent_id=req.explicit_agent_id or "",
        )

    @api_router.post(
        "/agents/route-bind",
        summary="Bind Agent Route Override",
        description="为一个 channel/chat/user 三元组写入 sticky agent route override。",
        tags=["agent"],
    )
    async def api_agents_route_bind(req: AgentRouteBindRequest):
        return _bind_agent_route(
            channel=req.channel,
            chat_id=req.chat_id,
            user_id=req.user_id,
            agent_id=req.agent_id,
        )

    @api_router.post(
        "/agents/route-clear",
        summary="Clear Agent Route Override",
        description="清除一个 channel/chat/user 三元组当前的 sticky agent route override。",
        tags=["agent"],
    )
    async def api_agents_route_clear(req: AgentRoutePreviewRequest):
        return _clear_agent_route(
            channel=req.channel,
            chat_id=req.chat_id,
            user_id=req.user_id,
        )

    @api_router.get(
        "/crawler/sources",
        summary="Crawler Source 清单",
        description="返回 crawler source catalog 中的已保存源定义以及最近的 source/run 事件摘要。",
        tags=["crawler"],
    )
    async def api_crawler_sources(source_file: str = ""):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import load_sources_json, resolve_sources_path

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir, source_file or None)
        sources = load_sources_json(catalog_path) if catalog_path.exists() else []
        recent_source_actions = sorted(
            [
                row
                for row in _read_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", limit=50)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )[:10]
        recent_runs = _read_recent_crawler_runs(data_dir=data_dir)
        return JSONResponse(
            {
                "catalog_path": str(catalog_path),
                "sources": [
                    {
                        "name": source.name,
                        "url": source.url,
                        "notebook_id": source.notebook_id,
                        "selector": source.selector,
                        "use_browser": bool(source.use_browser),
                        "max_chars": int(source.max_chars),
                        "metadata": dict(source.metadata or {}),
                        "enabled": bool(source.enabled),
                        "selector_type": str(source.selector_type or "css"),
                        "pagination_selector": str(source.pagination_selector or ""),
                        "max_pages": int(source.max_pages or 1),
                    }
                    for source in sources
                ],
                "total": len(sources),
                "recent_source_actions": recent_source_actions[:5],
                "source_actions_total": len(recent_source_actions),
                "recent_runs": recent_runs[:5],
                "runs_total": len(recent_runs),
            }
        )

    @api_router.post(
        "/crawler/sources",
        summary="保存 Crawler Source",
        description="将一个 crawler source 写入共享 catalog；同名 source 会被覆盖。",
        tags=["crawler"],
    )
    async def api_crawler_upsert_source(req: CrawlerSourceRequest):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.extractor import CrawlerSource
        from zen_claw.skills.crawler.scheduler import resolve_sources_path, upsert_source

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir)
        source = CrawlerSource(
            name=str(req.name or "").strip(),
            url=str(req.url or "").strip(),
            notebook_id=str(req.notebook_id or "default").strip() or "default",
            selector=str(req.selector or "").strip(),
            use_browser=bool(req.use_browser),
            max_chars=max(100, int(req.max_chars or 20_000)),
            metadata=dict(req.metadata or {}),
            selector_type=str(req.selector_type or "css").strip() or "css",
            pagination_selector=str(req.pagination_selector or "").strip(),
            max_pages=max(1, min(20, int(req.max_pages or 1))),
        )
        upsert_source(catalog_path, source)
        event = _append_crawler_source_audit(
            data_dir=data_dir,
            source=source,
            actor="api_key",
        )
        return JSONResponse(
            {
                "ok": True,
                "catalog_path": str(catalog_path),
                "source": {
                    "name": source.name,
                    "url": source.url,
                    "notebook_id": source.notebook_id,
                    "selector": source.selector,
                    "use_browser": bool(source.use_browser),
                    "max_chars": int(source.max_chars),
                    "metadata": dict(source.metadata or {}),
                    "enabled": bool(source.enabled),
                    "selector_type": str(source.selector_type or "css"),
                    "pagination_selector": str(source.pagination_selector or ""),
                    "max_pages": int(source.max_pages or 1),
                },
                "event": event,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source.name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.delete(
        "/crawler/sources/{source_name}",
        summary="删除 Crawler Source",
        description="从 catalog 中删除指定 source；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_delete_source(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import delete_source, resolve_sources_path

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir)
        found = delete_source(catalog_path, source_name)
        if not found:
            raise HTTPException(status_code=404, detail=f"source not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.source.delete",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "deleted": source_name,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.post(
        "/crawler/sources/{source_name}/disable",
        summary="禁用 Crawler Source",
        description="将指定 source 标记为 enabled=False；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_disable_source(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import resolve_sources_path, set_source_enabled

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir)
        found = set_source_enabled(catalog_path, source_name, False)
        if not found:
            raise HTTPException(status_code=404, detail=f"source not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.source.disable",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "source_name": source_name,
                "enabled": False,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.post(
        "/crawler/sources/{source_name}/enable",
        summary="启用 Crawler Source",
        description="将指定 source 标记为 enabled=True；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_enable_source(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import resolve_sources_path, set_source_enabled

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir)
        found = set_source_enabled(catalog_path, source_name, True)
        if not found:
            raise HTTPException(status_code=404, detail=f"source not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.source.enable",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "source_name": source_name,
                "enabled": True,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.get(
        "/crawler/sources/{source_name}/status",
        summary="Crawler Source 运行状态",
        description="返回指定 source 的元数据及最近运行摘要；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_source_status(source_name: str):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import get_source_by_name, resolve_sources_path

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir)
        try:
            source = get_source_by_name(catalog_path, source_name)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"source not found: {source_name}")
        all_runs = [
            row
            for row in _read_jsonl(data_dir / "dashboard" / "crawler_runs.log.jsonl", limit=200)
            if isinstance(row, dict)
            and str(row.get("source_name") or "").lower() == source_name.lower()
        ]
        all_runs.sort(key=lambda x: int(x.get("at_ms") or 0), reverse=True)
        recent = all_runs[:10]
        last = recent[0] if recent else {}
        return JSONResponse(
            {
                "source_name": source.name,
                "url": source.url,
                "enabled": bool(source.enabled),
                "last_run_at_ms": int(last.get("at_ms") or 0) or None,
                "last_run_status": str(last.get("status") or "") or None,
                "last_run_change_status": str(last.get("change_status") or "") or None,
                "last_run_mode": str(last.get("crawl_mode") or "") or None,
                "last_fetch_strategy": str(last.get("fetch_strategy") or "") or None,
                "last_attempt_count": int(last.get("attempt_count") or 0) or None,
                "last_retry_count": int(last.get("retry_count") or 0) or None,
                "last_pages_fetched": int(last.get("pages_fetched") or 0) or None,
                "last_paginated": bool(last.get("paginated")) if recent else None,
                "last_error": str(last.get("error") or "") or None,
                "last_run_documents": int(last.get("documents") or 0),
                "failed_runs": len(
                    [row for row in all_runs if str(row.get("status") or "").strip().lower() == "error"]
                ),
                "run_count": len(all_runs),
                "recent_runs": recent,
            }
        )

    @api_router.post(
        "/crawler/run",
        summary="执行 Crawler Source",
        description="按共享 catalog 中的 source name 触发一次 crawler 执行，并记录最近运行审计。",
        tags=["crawler"],
    )
    async def api_crawler_run(req: CrawlerRunRequest):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.extractor import CrawlerExtractor
        from zen_claw.skills.crawler.scheduler import get_source_by_name, resolve_sources_path

        data_dir = get_data_dir()
        catalog_path = resolve_sources_path(data_dir, req.source_file or None)
        source = get_source_by_name(catalog_path, req.source_name)
        extractor = CrawlerExtractor(
            data_dir=data_dir,
            tenant_id=str(req.tenant_id or "default").strip() or "default",
            store_kind=str(req.store_backend or "").strip(),
        )
        try:
            payload = await extractor.crawl_to_rag(source)
        except Exception as exc:
            _append_crawler_run_audit(
                data_dir=data_dir,
                source=source,
                payload={},
                actor="api_key",
                status="error",
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        event = _append_crawler_run_audit(
            data_dir=data_dir,
            source=source,
            payload=payload,
            actor="api_key",
            status="ok",
            error="",
        )
        return JSONResponse(
            {
                **payload,
                "event": event,
                "catalog_path": str(catalog_path),
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source.name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.get(
        "/crawler/schedules",
        summary="Crawler Schedules 列表",
        description="列出所有 crawler cron 调度配置。",
        tags=["crawler"],
    )
    async def api_crawler_schedules_list(
        tenant_id: str = "",
        source_name: str = "",
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import load_schedules_json, resolve_schedules_path

        data_dir = get_data_dir()
        schedules = load_schedules_json(resolve_schedules_path(data_dir))
        if tenant_id:
            schedules = [s for s in schedules if str(s.get("tenant_id") or "default") == tenant_id]
        if source_name:
            schedules = [
                s
                for s in schedules
                if str(s.get("source_name") or "").lower() == source_name.lower()
            ]
        run_rows = _read_recent_crawler_runs(data_dir=data_dir, limit=200)
        enriched: list[dict[str, Any]] = []
        for row in schedules:
            source_key = str(row.get("source_name") or "").strip().lower()
            all_runs = [
                item
                for item in run_rows
                if str(item.get("source_name") or "").strip().lower() == source_key
            ]
            recent_runs = all_runs[:10]
            last = recent_runs[0] if recent_runs else {}
            enriched.append(
                {
                    **dict(row),
                    "last_run_at_ms": int(last.get("at_ms") or 0) or None,
                    "last_run_status": str(last.get("status") or "") or None,
                    "last_run_change_status": str(last.get("change_status") or "") or None,
                    "last_run_mode": str(last.get("crawl_mode") or "") or None,
                    "last_fetch_strategy": str(last.get("fetch_strategy") or "") or None,
                    "last_attempt_count": int(last.get("attempt_count") or 0) or None,
                    "last_retry_count": int(last.get("retry_count") or 0) or None,
                    "last_pages_fetched": int(last.get("pages_fetched") or 0) or None,
                    "last_paginated": bool(last.get("paginated")) if recent_runs else None,
                    "last_error": str(last.get("error") or "") or None,
                    "run_count": len(all_runs),
                    "failed_runs": len(
                        [
                            item
                            for item in all_runs
                            if str(item.get("status") or "").strip().lower() == "error"
                        ]
                    ),
                }
            )
        return JSONResponse({"schedules": enriched, "total": len(enriched)})

    @api_router.post(
        "/crawler/schedules",
        summary="创建/更新 Crawler Schedule",
        description="为指定 source 创建或更新 cron 调度配置。",
        tags=["crawler"],
    )
    async def api_crawler_create_schedule(req: CrawlerScheduleRequest):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import (
            _validate_cron_expression,
            get_source_by_name,
            resolve_schedules_path,
            resolve_sources_path,
            upsert_schedule,
        )

        data_dir = get_data_dir()
        # Validate source exists
        catalog_path = resolve_sources_path(data_dir)
        try:
            get_source_by_name(catalog_path, req.source_name)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"source not found: {req.source_name}")
        # Validate cron expression
        if not _validate_cron_expression(req.cron):
            raise HTTPException(
                status_code=400,
                detail=f"invalid cron expression (must have 5 fields): {req.cron}",
            )
        schedule = {
            "source_name": req.source_name,
            "cron": req.cron,
            "tenant_id": req.tenant_id or "default",
            "store_backend": req.store_backend or "",
            "enabled": bool(req.enabled),
            "created_at_ms": int(_time.time() * 1000),
        }
        upsert_schedule(resolve_schedules_path(data_dir), schedule)
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.schedule.upsert",
            "source_name": req.source_name,
            "cron": req.cron,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "source_name": req.source_name,
                "cron": req.cron,
                "enabled": bool(req.enabled),
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=req.source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.get(
        "/crawler/schedules/{source_name}",
        summary="Crawler Schedule 状态",
        description="返回指定 source 的调度配置以及最近运行摘要；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_schedule_status(source_name: str):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import load_schedules_json, resolve_schedules_path

        data_dir = get_data_dir()
        rows = load_schedules_json(resolve_schedules_path(data_dir))
        target = next(
            (
                row
                for row in rows
                if str(row.get("source_name") or "").strip().lower() == source_name.strip().lower()
            ),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"schedule not found: {source_name}")
        run_rows = [
            row
            for row in _read_recent_crawler_runs(data_dir=data_dir, limit=200)
            if str(row.get("source_name") or "").strip().lower() == source_name.strip().lower()
        ]
        last = run_rows[0] if run_rows else {}
        return JSONResponse(
            {
                "schedule": dict(target),
                "last_run_at_ms": int(last.get("at_ms") or 0) or None,
                "last_run_status": str(last.get("status") or "") or None,
                "last_run_change_status": str(last.get("change_status") or "") or None,
                "last_run_mode": str(last.get("crawl_mode") or "") or None,
                "last_fetch_strategy": str(last.get("fetch_strategy") or "") or None,
                "last_attempt_count": int(last.get("attempt_count") or 0) or None,
                "last_retry_count": int(last.get("retry_count") or 0) or None,
                "last_pages_fetched": int(last.get("pages_fetched") or 0) or None,
                "last_paginated": bool(last.get("paginated")) if run_rows else None,
                "last_error": str(last.get("error") or "") or None,
                "failed_runs": len(
                    [row for row in run_rows if str(row.get("status") or "").strip().lower() == "error"]
                ),
                "run_count": len(run_rows),
                "recent_runs": run_rows[:10],
            }
        )

    @api_router.delete(
        "/crawler/schedules/{source_name}",
        summary="删除 Crawler Schedule",
        description="删除指定 source 的调度配置；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_delete_schedule(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import delete_schedule, resolve_schedules_path

        data_dir = get_data_dir()
        found = delete_schedule(resolve_schedules_path(data_dir), source_name)
        if not found:
            raise HTTPException(status_code=404, detail=f"schedule not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.schedule.delete",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "deleted": source_name,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.post(
        "/crawler/schedules/{source_name}/pause",
        summary="暂停 Crawler Schedule",
        description="将指定 source 的调度设为 enabled=False（暂停）；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_pause_schedule(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import resolve_schedules_path, set_schedule_enabled

        data_dir = get_data_dir()
        found = set_schedule_enabled(resolve_schedules_path(data_dir), source_name, False)
        if not found:
            raise HTTPException(status_code=404, detail=f"schedule not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.schedule.pause",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "source_name": source_name,
                "enabled": False,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.post(
        "/crawler/schedules/{source_name}/resume",
        summary="恢复 Crawler Schedule",
        description="将指定 source 的调度设为 enabled=True（恢复）；不存在则 404。",
        tags=["crawler"],
    )
    async def api_crawler_resume_schedule(source_name: str):
        import time as _time
        import uuid as _uuid

        from zen_claw.config.loader import get_data_dir
        from zen_claw.skills.crawler.scheduler import resolve_schedules_path, set_schedule_enabled

        data_dir = get_data_dir()
        found = set_schedule_enabled(resolve_schedules_path(data_dir), source_name, True)
        if not found:
            raise HTTPException(status_code=404, detail=f"schedule not found: {source_name}")
        event = {
            "at_ms": int(_time.time() * 1000),
            "trace_id": _uuid.uuid4().hex[:12],
            "event": "crawler.schedule.resume",
            "source_name": source_name,
            "actor": "api_key",
        }
        _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
        return JSONResponse(
            {
                "ok": True,
                "source_name": source_name,
                "enabled": True,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="crawler",
                    target=source_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )

    @api_router.get(
        "/model-routing/summary",
        summary="Model Routing 深度聚合",
        description=(
            "返回 model routing 的深度聚合报告，包含跨维度 pivot（每 model 的 reason/intent/channel 分布）"
            "和可配置粒度趋势（hour/day/week）。"
        ),
        tags=["agent"],
    )
    async def api_model_routing_summary(
        model: str = "",
        channel: str = "",
        intent: str = "",
        since_hours: int = 0,
        from_at_ms: int = 0,
        to_at_ms: int = 0,
        bucket_unit: str = "hour",
        max_groups: int = 10,
    ):
        from zen_claw.config.loader import get_data_dir

        rows = sorted(
            [
                row
                for row in _read_jsonl(get_data_dir() / "dashboard" / "model_routing.log.jsonl", limit=5000)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )
        norm_model = str(model or "").strip().lower()
        norm_channel = str(channel or "").strip().lower()
        norm_intent = str(intent or "").strip().lower()
        bounded_since = max(0, min(int(since_hours or 0), 24 * 365))
        bounded_from = max(0, int(from_at_ms or 0))
        bounded_to = max(0, int(to_at_ms or 0))
        bounded_max = max(1, min(int(max_groups or 10), 50))

        if norm_model:
            rows = [r for r in rows if str(r.get("selected_model") or "").strip().lower() == norm_model]
        if norm_channel:
            rows = [r for r in rows if str(r.get("channel") or "").strip().lower() == norm_channel]
        if norm_intent:
            rows = [r for r in rows if str(r.get("intent_name") or "").strip().lower() == norm_intent]
        if bounded_since:
            min_at_ms = int(time.time() * 1000) - bounded_since * 3_600_000
            rows = [r for r in rows if int(r.get("at_ms") or 0) >= min_at_ms]
        if bounded_from:
            rows = [r for r in rows if int(r.get("at_ms") or 0) >= bounded_from]
        if bounded_to:
            rows = [r for r in rows if int(r.get("at_ms") or 0) <= bounded_to]

        summary = _build_model_routing_deep_summary(rows, bucket_unit=bucket_unit, max_groups=bounded_max)
        return JSONResponse(summary)

    @api_router.get(
        "/model-routing",
        summary="Model Routing 事件",
        description="返回最近的 model routing 事件，并支持按 model/reason 过滤。",
        tags=["agent"],
    )
    async def api_model_routing(
        model: str = "",
        reason: str = "",
        limit: int = 20,
        offset: int = 0,
        since_hours: int = 0,
        from_at_ms: int = 0,
        to_at_ms: int = 0,
        bucket_minutes: int = 60,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        normalized_model = str(model or "").strip().lower()
        normalized_reason = str(reason or "").strip().lower()
        bounded_limit = max(1, min(int(limit or 20), 50))
        bounded_offset = max(0, int(offset or 0))
        bounded_since_hours = max(0, min(int(since_hours or 0), 24 * 365))
        bounded_from_at_ms = max(0, int(from_at_ms or 0))
        bounded_to_at_ms = max(0, int(to_at_ms or 0))
        bounded_bucket_minutes = max(5, min(int(bucket_minutes or 60), 24 * 60))
        fmt = str(format or "json").strip().lower()
        rows = sorted(
            [
                row
                for row in _read_jsonl(get_data_dir() / "dashboard" / "model_routing.log.jsonl", limit=100)
                if isinstance(row, dict)
            ],
            key=lambda x: int(x.get("at_ms") or 0),
            reverse=True,
        )
        if normalized_model:
            rows = [
                row
                for row in rows
                if str(row.get("selected_model") or "").strip().lower() == normalized_model
            ]
        if normalized_reason:
            rows = [
                row for row in rows if str(row.get("reason") or "").strip().lower() == normalized_reason
            ]
        if bounded_since_hours:
            min_at_ms = int(time.time() * 1000) - (bounded_since_hours * 3600 * 1000)
            rows = [row for row in rows if int(row.get("at_ms") or 0) >= min_at_ms]
        if bounded_from_at_ms:
            rows = [row for row in rows if int(row.get("at_ms") or 0) >= bounded_from_at_ms]
        if bounded_to_at_ms:
            rows = [row for row in rows if int(row.get("at_ms") or 0) <= bounded_to_at_ms]
        summary = _build_model_routing_summary(
            rows,
            bucket_minutes=bounded_bucket_minutes,
            max_groups=8,
        )
        sliced_rows = rows[bounded_offset : bounded_offset + bounded_limit]
        if fmt == "csv":
            header = ["at_ms", "trace_id", "channel", "chat_id", "intent_name", "selected_model", "reason"]
            lines = [",".join(header)]
            for row in sliced_rows:
                values = []
                for key in header:
                    value = str(row.get(key) or "")
                    if any(ch in value for ch in [",", "\"", "\n", "\r"]):
                        value = '"' + value.replace('"', '""') + '"'
                    values.append(value)
                lines.append(",".join(values))
            csv_payload = "\n".join(lines) + "\n"
            return StreamingResponse(
                iter([csv_payload]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=model-routing-events.csv"},
            )
        return JSONResponse(
            {
                "events": sliced_rows,
                "summary": summary,
                "total": len(rows),
                "limit": bounded_limit,
                "offset": bounded_offset,
                "returned": len(sliced_rows),
                "filters": {
                    "model": str(model or ""),
                    "reason": str(reason or ""),
                    "since_hours": bounded_since_hours,
                    "from_at_ms": bounded_from_at_ms,
                    "to_at_ms": bounded_to_at_ms,
                    "bucket_minutes": bounded_bucket_minutes,
                    "format": fmt,
                },
            }
        )

    @api_router.get(
        "/ops/summary",
        summary="Operations Summary 汇总",
        description="返回跨 agent / skill / channel / model routing 的运营汇总，并支持 CSV 导出。",
        tags=["dashboard"],
    )
    async def api_ops_summary(
        format: str = "",
        surface: str = "",
        actor: str = "",
        target: str = "",
        pending_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ):
        from zen_claw.config.loader import get_data_dir, load_config

        fmt = str(format or "json").strip().lower()
        report = _build_control_plane_report(
            data_dir=get_data_dir(),
            snapshot=build_dashboard_snapshot(load_config()),
            surface=surface,
            actor=actor,
            target=target,
            pending_only=pending_only,
            limit=limit,
            offset=offset,
        )
        if fmt == "csv":
            csv_payload = _ops_summary_report_csv(report)
            return StreamingResponse(
                iter([csv_payload]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=operations-summary.csv"},
            )
        return JSONResponse(report)

    @api_router.post(
        "/ops/apply-pending",
        summary="确认 Pending Control-Plane 变更已应用",
        description="统一记录 agent/channel pending 配置变更已被 operator 应用，用于跨 surface 清除 pending apply 视图。",
        tags=["dashboard"],
    )
    async def api_ops_apply_pending(surface: str = "", actor: str = "", target: str = ""):
        from zen_claw.config.loader import get_data_dir, load_config

        data_dir = get_data_dir()
        snapshot = build_dashboard_snapshot(load_config())
        before_report = _build_control_plane_report(
            data_dir=data_dir,
            snapshot=snapshot,
            surface=surface,
            actor=actor,
            target=target,
            pending_only=True,
            limit=100,
            offset=0,
        )
        payload = await _apply_pending_control_plane_actions(
            data_dir=data_dir,
            actor="api_key",
            pending_rows=list(before_report.get("pending_apply") or []),
            surface=surface,
            actor_filter=actor,
            target=target,
        )
        after_snapshot = build_dashboard_snapshot(load_config())
        after_report = _build_control_plane_report(
            data_dir=data_dir,
            snapshot=after_snapshot,
            surface=surface,
            actor=actor,
            target=target,
            pending_only=True,
            limit=100,
            offset=0,
        )
        payload["pending_before"] = int(before_report.get("pending_apply_total") or 0)
        payload["pending_after"] = int(after_report.get("pending_apply_total") or 0)
        payload["cleared_count"] = max(0, payload["pending_before"] - payload["pending_after"])
        payload["remaining_targets"] = [
            str(row.get("target") or "") for row in list(after_report.get("pending_apply") or [])[:10]
        ]
        return JSONResponse(payload)

    @api_router.get(
        "/ops/apply-plan",
        summary="预览 Pending Control-Plane Apply 计划",
        description="返回当前筛选条件下会被统一 apply acknowledgement 命中的 pending rows，但不会写入任何 apply 审计。",
        tags=["dashboard"],
    )
    async def api_ops_apply_plan(surface: str = "", actor: str = "", target: str = ""):
        from zen_claw.config.loader import get_data_dir, load_config

        data_dir = get_data_dir()
        snapshot = build_dashboard_snapshot(load_config())
        report = _build_control_plane_report(
            data_dir=data_dir,
            snapshot=snapshot,
            surface=surface,
            actor=actor,
            target=target,
            pending_only=True,
            limit=100,
            offset=0,
        )
        return JSONResponse(
            _build_pending_apply_plan(
                pending_rows=list(report.get("pending_apply") or []),
                surface=surface,
                actor_filter=actor,
                target=target,
            )
        )

    @api_router.post(
        "/ops/reload-execute",
        summary="执行 Reload",
        description="对支持的 surface 执行真实的配置 reload，区分 in_process / config_reload / audit_only；dry_run=True 时只返回计划不执行。",
        tags=["dashboard"],
    )
    async def api_ops_reload_execute(req: OpsReloadExecuteRequest):
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        plan = _build_reload_execute_plan(cfg, surface=req.surface, target=req.target)
        if req.dry_run:
            return JSONResponse({"dry_run": True, "plan": plan, "executed": 0, "audit_only": 0, "unsupported": 0})

        results: list[dict[str, Any]] = []
        executed = 0
        audit_only_count = 0
        unsupported_count = 0
        for entry in plan:
            mode = str(entry.get("execution_mode") or "unsupported")
            surface_name = str(entry.get("surface") or "")
            target_name = str(entry.get("target") or "")
            ok = True
            error = ""
            try:
                if mode == "config_reload":
                    load_config()  # reload in-memory config
                    executed += 1
                elif mode == "in_process":
                    # Attempt in-process restart for webchat runtime if callable
                    restart_fn = globals().get("_restart_webchat_runtime") or getattr(
                        __builtins__, "_restart_webchat_runtime", None
                    )
                    if callable(restart_fn):
                        restart_fn(cfg)
                    else:
                        load_config()  # fallback: config reload
                    executed += 1
                elif mode == "audit_only":
                    audit_only_count += 1
                else:
                    unsupported_count += 1
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = str(exc)
            results.append({
                "surface": surface_name,
                "target": target_name,
                "execution_mode": mode,
                "ok": ok,
                "error": error,
            })
            _append_reload_execute_audit(
                data_dir=data_dir,
                surface=surface_name,
                target=target_name,
                execution_mode=mode,
                ok=ok,
                error=error,
                dry_run=False,
            )
        return JSONResponse({
            "dry_run": False,
            "executed": executed,
            "audit_only": audit_only_count,
            "unsupported": unsupported_count,
            "results": results,
        })

    @api_router.get(
        "/channels",
        summary="Channel 清单",
        description="返回当前 channel registry 的统一运营摘要和最近配置动作。",
        tags=["channels"],
    )
    async def api_channels():
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        data_dir = get_data_dir()
        rows = [build_channel_rbac_row(cfg, spec) for spec in iter_channel_specs()]
        pending_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "channel_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "channel_apply.log.jsonl",
            target_field="channel_name",
        )
        recent_actions = list(pending_state["recent_actions"])
        pending = list(pending_state["pending"])
        return JSONResponse(
            {
                "channels": rows,
                "total": len(rows),
                "enabled": len([row for row in rows if row["enabled"]]),
                "alpha_ready": len([row for row in rows if row.get("alpha_contract_ready")]),
                "alpha_gaps": len([row for row in rows if not row.get("alpha_contract_ready")]),
                "runtime_controls": _build_channel_runtime_control_rows(cfg),
                "recent_actions": recent_actions[:5],
                "actions_total": len(recent_actions),
                "pending_reload": len(pending) > 0,
                "pending_reload_count": len(pending),
                "last_apply": pending_state["last_apply"],
            }
        )

    @api_router.get(
        "/channels/history",
        summary="Channel Action History",
        description="返回 channel 配置动作历史，支持按 channel/action/actor 过滤和 CSV 导出。",
        tags=["channels"],
    )
    async def api_channels_history(
        channel_name: str = "",
        action: str = "",
        actor: str = "",
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        payload = _read_channel_action_history(
            data_dir=get_data_dir(),
            channel_name=channel_name,
            action=action,
            actor=actor,
            limit=limit,
            offset=offset,
        )
        if str(format or "").strip().lower() == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    "at_ms",
                    "channel_name",
                    "action",
                    "enabled",
                    "actor",
                    "detail",
                    "trace_id",
                ]
            )
            for row in list(payload.get("rows") or []):
                writer.writerow(
                    [
                        row.get("at_ms"),
                        row.get("channel_name"),
                        row.get("action"),
                        row.get("enabled"),
                        row.get("actor"),
                        row.get("detail"),
                        row.get("trace_id"),
                    ]
                )
            return StreamingResponse(
                iter([buffer.getvalue().encode("utf-8")]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=channel-action-history.csv"},
            )
        return JSONResponse(payload)

    @api_router.post(
        "/channels/{name}/enable",
        summary="启用 Channel",
        description="启用一个 channel 配置项；当前为配置级动作，通常需要后续 reload 才会生效。",
        tags=["channels"],
    )
    async def api_channels_enable(name: str):
        return _update_channel_enabled(name=name, enabled=True)

    @api_router.post(
        "/channels/{name}/disable",
        summary="停用 Channel",
        description="停用一个 channel 配置项；当前为配置级动作，通常需要后续 reload 才会生效。",
        tags=["channels"],
    )
    async def api_channels_disable(name: str):
        return _update_channel_enabled(name=name, enabled=False)

    @api_router.post(
        "/channels/reload-ack",
        summary="确认 Channel 变更已应用",
        description="记录运维已在进程外完成 channel 配置变更的应用或 reload；该动作不会直接重启本进程。",
        tags=["channels"],
    )
    async def api_channels_reload_ack():
        from zen_claw.config.loader import get_data_dir

        data_dir = get_data_dir()
        before_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "channel_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "channel_apply.log.jsonl",
            target_field="channel_name",
        )
        pending_channels = {
            str(row.get("channel_name") or "").strip().lower()
            for row in list(before_state.get("pending") or [])
            if isinstance(row, dict)
        }
        runtime_execution = None
        if "webchat" in pending_channels:
            runtime_execution = await _execute_channel_runtime_apply("webchat")
            if str(runtime_execution.get("status") or "").strip().lower() == "failed":
                raise HTTPException(
                    status_code=503,
                    detail=str(runtime_execution.get("error") or "channel runtime restart failed"),
                )
        event = _append_channel_apply_audit(
            data_dir=data_dir,
            actor="api_key",
            note="operator_confirmed_reload",
        )
        after_state = _collect_pending_apply_state(
            data_dir=data_dir,
            actions_path=data_dir / "dashboard" / "channel_actions.log.jsonl",
            apply_path=data_dir / "dashboard" / "channel_apply.log.jsonl",
            target_field="channel_name",
        )
        return JSONResponse(
            {
                "ok": True,
                "applied": True,
                "event": event,
                "pending_before": int(before_state["pending_count"]),
                "pending_after": int(after_state["pending_count"]),
                "cleared_count": max(
                    0, int(before_state["pending_count"]) - int(after_state["pending_count"])
                ),
                "runtime_execution": runtime_execution,
            }
        )

    @api_router.get(
        "/channels/runtime",
        summary="Channel Runtime 状态",
        description="返回当前 dashboard 进程内可控 channel runtime 的运行状态。",
        tags=["channels"],
    )
    async def api_channels_runtime():
        from zen_claw.config.loader import load_config

        return JSONResponse(
            {
                "rows": _build_channel_runtime_control_rows(load_config()),
                "total": len(iter_channel_specs()),
            }
        )

    @api_router.post(
        "/channels/{name}/runtime/start",
        summary="启动 Channel Runtime",
        description="启动当前 dashboard 进程内支持的 channel runtime；具体能力由 channel registry 声明。",
        tags=["channels"],
    )
    async def api_channels_runtime_start(name: str):
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        channel_name = str(name or "").strip().lower()
        runtime_rows = {
            str(row.get("channel_name") or "").strip().lower(): row
            for row in _build_channel_runtime_control_rows(cfg)
        }
        handler = _channel_runtime_action_handlers().get(channel_name, {}).get("start")
        if handler is None:
            raise HTTPException(status_code=400, detail=f"runtime control unsupported: {channel_name}")
        summary = await handler()
        runtime_row = runtime_rows.get(channel_name) or {}
        event = _append_channel_action_audit(
            data_dir=get_data_dir(),
            channel_name=channel_name,
            action="runtime_start",
            enabled=bool(runtime_row.get("enabled")),
            actor="api_key",
            detail=f"state={summary['state']}; running_instances={summary['running_instances']}",
        )
        return JSONResponse({"ok": True, "runtime": summary, "event": event})

    @api_router.post(
        "/channels/{name}/runtime/stop",
        summary="停止 Channel Runtime",
        description="停止当前 dashboard 进程内支持的 channel runtime；具体能力由 channel registry 声明。",
        tags=["channels"],
    )
    async def api_channels_runtime_stop(name: str):
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        channel_name = str(name or "").strip().lower()
        runtime_rows = {
            str(row.get("channel_name") or "").strip().lower(): row
            for row in _build_channel_runtime_control_rows(cfg)
        }
        handler = _channel_runtime_action_handlers().get(channel_name, {}).get("stop")
        if handler is None:
            raise HTTPException(status_code=400, detail=f"runtime control unsupported: {channel_name}")
        summary = await handler()
        runtime_row = runtime_rows.get(channel_name) or {}
        event = _append_channel_action_audit(
            data_dir=get_data_dir(),
            channel_name=channel_name,
            action="runtime_stop",
            enabled=bool(runtime_row.get("enabled")),
            actor="api_key",
            detail=(
                f"state={summary['state']}; running_instances={summary['running_instances']}; "
                f"stopped_instances={summary.get('stopped_instances', 0)}"
            ),
        )
        return JSONResponse({"ok": True, "runtime": summary, "event": event})

    @api_router.post(
        "/skills/{name}/enable",
        summary="启用技能",
        description="启用一个已发现的 skill，并返回更新后的库存摘要。",
        tags=["skills"],
    )
    async def api_skills_enable(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        if not loader.set_skill_enabled(name, True):
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        _append_skill_action_audit(
            data_dir=get_data_dir(),
            skill_name=name,
            action="enable",
            enabled=True,
            actor="api_key",
        )
        apply_state = _build_apply_state(
            surface="skill",
            target=name,
            reload_required=False,
            execution_mode="in_process",
        )
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "enabled": True,
                "reload_required": False,
                "apply_state": apply_state,
            }
        )

    @api_router.post(
        "/skills/{name}/disable",
        summary="停用技能",
        description="停用一个已发现的 skill，并返回更新后的库存摘要。",
        tags=["skills"],
    )
    async def api_skills_disable(name: str):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        if not loader.set_skill_enabled(name, False):
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        _append_skill_action_audit(
            data_dir=get_data_dir(),
            skill_name=name,
            action="disable",
            enabled=False,
            actor="api_key",
        )
        apply_state = _build_apply_state(
            surface="skill",
            target=name,
            reload_required=False,
            execution_mode="in_process",
        )
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "enabled": False,
                "reload_required": False,
                "apply_state": apply_state,
            }
        )

    @api_router.post(
        "/skills/enable-batch",
        summary="批量启用技能",
        description="批量启用多个已发现的 skill，并为每个变更记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_enable_batch(req: SkillBatchStateRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        payload = _set_skill_enabled_batch(loader=loader, names=list(req.names or []), enabled=True)
        for row in list(payload.get("rows") or []):
            _append_skill_action_audit(
                data_dir=get_data_dir(),
                skill_name=str(row.get("name") or ""),
                action="enable",
                enabled=True,
                actor="api_key",
                detail="batch enable",
            )
        return JSONResponse(payload)

    @api_router.post(
        "/skills/disable-batch",
        summary="批量停用技能",
        description="批量停用多个已发现的 skill，并为每个变更记录审计事件。",
        tags=["skills"],
    )
    async def api_skills_disable_batch(req: SkillBatchStateRequest):
        from zen_claw.agent.skills import SkillsLoader
        from zen_claw.config.loader import get_data_dir, load_config

        cfg = load_config()
        loader = SkillsLoader(cfg.workspace_path)
        payload = _set_skill_enabled_batch(loader=loader, names=list(req.names or []), enabled=False)
        for row in list(payload.get("rows") or []):
            _append_skill_action_audit(
                data_dir=get_data_dir(),
                skill_name=str(row.get("name") or ""),
                action="disable",
                enabled=False,
                actor="api_key",
                detail="batch disable",
            )
        return JSONResponse(payload)

    @api_router.post(
        "/skills/content-gen/run",
        summary="直接运行内容生成 Skill",
        description=(
            "直接调用 content_gen skill 的生成管线，无需通过 agent 对话。"
            "支持所有渠道模板、可选 RAG 知识接地和合规预检。"
            "未配置 LLM provider 时自动退回模板模式。"
        ),
        tags=["skills"],
    )
    async def api_skills_content_gen_run(req: ContentGenRunRequest, request: Request):
        from zen_claw.auth.paths import DEFAULT_TENANT_ID
        from zen_claw.config.loader import get_data_dir, load_config
        from zen_claw.skills.compliance_check.checker import ComplianceChecker
        from zen_claw.skills.content_gen.generator import ContentGenerator
        from zen_claw.skills.rag_retrieve.retriever import RAGRetrieverSkill

        try:
            data_dir = get_data_dir()
            tenant_id = str(req.tenant_id or "").strip() or str(
                getattr(request.state, "tenant_id", "") or ""
            ).strip() or DEFAULT_TENANT_ID

            try:
                cfg = load_config()
                from zen_claw.providers.factory import create_provider

                provider = create_provider(cfg)
            except Exception:
                provider = None

            rag = (
                RAGRetrieverSkill(data_dir, tenant_id=tenant_id)
                if req.use_rag
                else None
            )
            checker = ComplianceChecker(provider=provider)
            generator = ContentGenerator(
                provider=provider,
                compliance_checker=checker,
                rag_retriever=rag,
            )
            result = await generator.generate(
                industry=str(req.industry or "").strip(),
                channel=str(req.channel or "generic").strip(),
                product_name=str(req.product_name or "").strip(),
                audience=str(req.audience or "").strip(),
                style=str(req.style or "").strip(),
                key_points=list(req.key_points or []),
                count=int(req.count or 3),
                use_rag=bool(req.use_rag),
                notebook_id=str(req.notebook_id or "").strip(),
                rag_tenant_id=tenant_id,
            )
            execution = dict(result.get("execution") or {})
            execution.update(
                {
                    "skill": "content_gen",
                    "api_mode": "direct_run",
                    "tenant_id": tenant_id,
                    "channel": str(req.channel or "generic").strip(),
                    "rag_requested": bool(req.use_rag),
                }
            )
            return JSONResponse(
                {
                    "ok": True,
                    **result,
                    "tenant_id": tenant_id,
                    "execution": execution,
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"content generation failed: {exc}") from exc

    @api_router.post(
        "/skills/compliance-check/run",
        summary="直接运行合规审查 Skill",
        description=(
            "直接调用 compliance_check skill 的审查管线，无需通过 agent 对话。"
            "默认仅运行规则引擎（Layer 1+2），设置 semantic=true 时额外调用 LLM 语义审查层。"
        ),
        tags=["skills"],
    )
    async def api_skills_compliance_check_run(req: ComplianceCheckRunRequest):
        from zen_claw.config.loader import load_config
        from zen_claw.skills.compliance_check.checker import ComplianceChecker

        try:
            provider = None
            if req.semantic:
                try:
                    cfg = load_config()
                    from zen_claw.providers.factory import create_provider

                    provider = create_provider(cfg)
                except Exception:
                    provider = None

            checker = ComplianceChecker(provider=provider if req.semantic else None)
            if req.semantic:
                result = await checker.review_text(
                    str(req.text or ""),
                    industry=str(req.industry or "").strip(),
                    channel=str(req.channel or "").strip(),
                    policy_context=str(req.policy_context or "").strip(),
                )
            else:
                result = checker.check_text(
                    str(req.text or ""),
                    industry=str(req.industry or "").strip(),
                    channel=str(req.channel or "").strip(),
                )
            return JSONResponse({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"compliance check failed: {exc}") from exc

    @api_router.get(
        "/rag/notebooks",
        summary="Notebook 列表",
        description="返回当前 tenant 下所有 notebook，含每个 notebook 的 doc_count 和 backend 健康状态。",
        tags=["rag"],
    )
    async def api_rag_list_notebooks(
        tenant_id: str = "",
        limit: int = 20,
        offset: int = 0,
        request: Request = None,  # type: ignore[assignment]
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        resolved_tenant = _resolve_rag_tenant_id(request, tenant_id) if request else (tenant_id or "default")
        try:
            pipeline = RAGPipeline(get_data_dir(), tenant_id=resolved_tenant)
            all_rows = pipeline.list_notebooks()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        safe_limit = max(1, min(100, int(limit or 20)))
        safe_offset = max(0, int(offset or 0))
        page = all_rows[safe_offset : safe_offset + safe_limit]
        return JSONResponse({"notebooks": page, "total": len(all_rows), "tenant_id": resolved_tenant})

    @api_router.post(
        "/rag/notebooks",
        summary="创建 Notebook",
        description="显式创建一个 notebook 元数据记录。notebook_id 已存在时返回 409。",
        tags=["rag"],
    )
    async def api_rag_create_notebook(req: RagNotebookCreateRequest, request: Request):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        resolved_tenant = _resolve_rag_tenant_id(request, str(req.tenant_id or ""))
        try:
            pipeline = RAGPipeline(get_data_dir(), tenant_id=resolved_tenant)
            result = pipeline.create_notebook(
                str(req.notebook_id or "").strip(),
                display_name=str(req.display_name or ""),
                store_backend=str(req.store_backend or ""),
            )
        except ValueError as exc:
            msg = str(exc)
            if "already exists" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(result)

    @api_router.post(
        "/rag/notebook/{notebook_id}/repair",
        summary="修复 Notebook Backend",
        description="尝试对指定 notebook 的 backend 执行修复（reindex/metadata reset）并返回修复报告。",
        tags=["rag"],
    )
    async def api_rag_repair_notebook(notebook_id: str, tenant_id: str = "", request: Request = None):  # type: ignore[assignment]
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        resolved_tenant = _resolve_rag_tenant_id(request, tenant_id) if request else (tenant_id or "default")
        try:
            pipeline = RAGPipeline(get_data_dir(), tenant_id=resolved_tenant)
            result = pipeline.repair_notebook(notebook_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(result)

    @api_router.post(
        "/rag/ingest",
        summary="RAG 入库",
        description="将文件、目录或 URL 通过当前知识管线入库到指定 notebook。",
        tags=["rag"],
    )
    async def api_rag_ingest(req: RAGIngestRequest, request: Request):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = await RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, str(req.tenant_id or "")),
                store_kind=str(req.store_backend or ""),
            ).ingest_with_metadata(
                req.source,
                notebook_id=str(req.notebook_id or ""),
                metadata=dict(req.metadata or {}),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.post(
        "/rag/search",
        summary="RAG 检索",
        description="通过统一 RAG pipeline 对 notebook 做搜索。",
        tags=["rag"],
    )
    async def api_rag_search(req: RAGSearchRequest, request: Request):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline
        from zen_claw.knowledge.reranker import build_reranker

        reranker = None
        if req.rerank:
            try:
                from zen_claw.config.loader import load_config
                from zen_claw.providers.factory import create_provider

                reranker = build_reranker(create_provider(load_config()))
            except Exception:
                reranker = None

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, str(req.tenant_id or "")),
                store_kind=str(req.store_backend or ""),
                reranker=reranker,
            ).search(
                req.query,
                notebook_id=str(req.notebook_id or ""),
                top_k=req.top_k,
                source_contains=str(req.source_contains or ""),
                filters=dict(req.filters or {}),
                rerank=req.rerank,
                rerank_top_n=req.rerank_top_n,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/documents",
        summary="RAG 文档列表",
        description="返回指定 notebook 下已登记的文档列表和稳定 document_id。",
        tags=["rag"],
    )
    async def api_rag_documents(
        request: Request, notebook_id: str = "", tenant_id: str = "", store_backend: str = ""
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).list_documents(notebook_id=notebook_id)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/stats",
        summary="RAG 统计",
        description="返回 notebook / documents / chunks 的聚合统计。",
        tags=["rag"],
    )
    async def api_rag_stats(
        request: Request, notebook_id: str = "", tenant_id: str = "", store_backend: str = ""
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).stats(notebook_id=notebook_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.delete(
        "/rag/doc/{document_id:path}",
        summary="RAG 文档删除",
        description="按稳定 document_id 删除指定 notebook 下的文档及其向量块。",
        tags=["rag"],
    )
    async def api_rag_delete_document(
        request: Request,
        document_id: str,
        notebook_id: str = "",
        tenant_id: str = "",
        store_backend: str = "",
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).delete_document(document_id, notebook_id=notebook_id)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.delete(
        "/rag/notebook/{notebook_id}/documents",
        summary="RAG Notebook 清空",
        description="删除指定 notebook 下的全部文档和向量块，但保留 notebook 元数据。",
        tags=["rag"],
    )
    async def api_rag_clear_notebook(
        request: Request, notebook_id: str, tenant_id: str = "", store_backend: str = ""
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).clear_notebook(notebook_id=notebook_id)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    # ── B4: Document preview + keyword search ────────────────────────────────

    @api_router.get(
        "/rag/documents/{document_id}/preview",
        summary="RAG 文档内容预览",
        description="返回文档原始内容前 2000 字符，用于 Dashboard 快速预览。",
        tags=["rag"],
    )
    async def api_rag_document_preview(
        request: Request,
        document_id: str,
        notebook_id: str = "",
        tenant_id: str = "",
        store_backend: str = "",
        max_chars: int = 2000,
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).preview_document(
                document_id,
                notebook_id=notebook_id,
                max_chars=max(1, min(int(max_chars), 50000)),
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/documents/search",
        summary="RAG 文档关键词搜索",
        description="按关键词过滤已入库文档列表（匹配 source 文件名）。",
        tags=["rag"],
    )
    async def api_rag_documents_search(
        request: Request,
        q: str = "",
        notebook_id: str = "",
        tenant_id: str = "",
        store_backend: str = "",
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=_resolve_rag_tenant_id(request, tenant_id),
                store_kind=store_backend,
            ).search_documents(q, notebook_id=notebook_id)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.post(
        "/rag/retention/run",
        summary="RAG Retention 执行",
        description="对指定 notebook 执行保留策略清理，支持按最大文档数和最大保留天数裁剪。",
        tags=["rag"],
    )
    async def api_rag_retention_run(req: RAGRetentionRequest, request: Request):
        from zen_claw.config.loader import get_data_dir, load_config
        from zen_claw.knowledge.pipeline import RAGPipeline

        cfg = load_config()
        try:
            resolved_tenant_id = _resolve_rag_tenant_id(request, str(req.tenant_id or ""))
            notebook_policy: dict[str, Any] = {}
            if str(req.notebook_id or "").strip():
                try:
                    notebook_policy = _read_notebook_policy(resolved_tenant_id, str(req.notebook_id or ""))
                except Exception:
                    notebook_policy = {}
            max_documents = int(
                req.max_documents
                or notebook_policy.get("retention_max_documents")
                or cfg.knowledge.retention_max_documents
                or 0
            )
            max_age_days = int(
                req.max_age_days
                or notebook_policy.get("retention_max_age_days")
                or cfg.knowledge.retention_max_age_days
                or 0
            )
            payload = RAGPipeline(
                get_data_dir(),
                tenant_id=resolved_tenant_id,
                store_kind=str(req.store_backend or ""),
            ).run_retention(
                notebook_id=str(req.notebook_id or ""),
                max_documents=max_documents,
                max_age_days=max_age_days,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/tenant-policy",
        summary="Tenant RAG Backend Policy",
        description="返回指定 tenant 的 RAG backend policy。已认证多租户请求会沿用 fail-closed tenant 约束。",
        tags=["rag"],
    )
    async def api_rag_tenant_policy(request: Request, tenant_id: str = ""):
        try:
            resolved_tenant_id = _resolve_rag_tenant_id(request, tenant_id)
            payload = _read_tenant_policy(resolved_tenant_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/notebook-policy",
        summary="Notebook RAG Backend Policy",
        description="返回指定 tenant 下 notebook 的 RAG backend policy。",
        tags=["rag"],
    )
    async def api_rag_notebook_policy(
        request: Request, notebook_id: str, tenant_id: str = ""
    ):
        try:
            resolved_tenant_id = _resolve_rag_tenant_id(request, tenant_id)
            payload = _read_notebook_policy(resolved_tenant_id, notebook_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.get(
        "/rag/activity-history",
        summary="Knowledge Activity History",
        description="返回聚合后的 knowledge policy/cron 活动历史，并支持过滤与 CSV 导出。",
        tags=["rag"],
    )
    async def api_rag_activity_history(
        event_kind: str = "",
        tenant_id: str = "",
        actor: str = "",
        status: str = "",
        notebook_id: str = "",
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        payload = _read_knowledge_activity_history(
            data_dir=get_data_dir(),
            event_kind=event_kind,
            tenant_id=tenant_id,
            actor=actor,
            status=status,
            notebook_id=notebook_id,
            limit=limit,
            offset=offset,
        )
        if str(format or "").strip().lower() == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                ["at_ms", "event_kind", "tenant_id", "target_id", "actor", "status", "detail", "trace_id"]
            )
            for row in list(payload.get("rows") or []):
                writer.writerow(
                    [
                        row.get("at_ms"),
                        row.get("event_kind"),
                        row.get("tenant_id"),
                        row.get("target_id"),
                        row.get("actor"),
                        row.get("status"),
                        row.get("detail"),
                        row.get("trace_id"),
                    ]
                )
            return StreamingResponse(
                iter([buffer.getvalue().encode("utf-8")]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=knowledge-activity-history.csv"},
            )
        return JSONResponse(payload)

    @api_router.get(
        "/rag/policy-history",
        summary="Knowledge Policy Change History",
        description="返回 knowledge backend policy 变更历史，支持按 kind/actor/tenant 过滤和 CSV 导出。",
        tags=["rag"],
    )
    async def api_rag_policy_history(
        policy_kind: str = "",
        actor: str = "",
        tenant_id: str = "",
        target_id: str = "",
        before_store_backend: str = "",
        after_store_backend: str = "",
        limit: int = 20,
        offset: int = 0,
        format: str = "",
    ):
        from zen_claw.config.loader import get_data_dir

        payload = _read_knowledge_policy_history(
            data_dir=get_data_dir(),
            policy_kind=policy_kind,
            actor=actor,
            tenant_id=tenant_id,
            target_id=target_id,
            before_store_backend=before_store_backend,
            after_store_backend=after_store_backend,
            limit=limit,
            offset=offset,
        )
        if str(format or "").strip().lower() == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    "at_ms",
                    "policy_kind",
                    "tenant_id",
                    "target_id",
                    "before_store_backend",
                    "after_store_backend",
                    "actor",
                    "trace_id",
                ]
            )
            for row in list(payload.get("rows") or []):
                writer.writerow(
                    [
                        row.get("at_ms"),
                        row.get("policy_kind"),
                        row.get("tenant_id"),
                        row.get("target_id"),
                        row.get("before_store_backend"),
                        row.get("after_store_backend"),
                        row.get("actor"),
                        row.get("trace_id"),
                    ]
                )
            return StreamingResponse(
                iter([buffer.getvalue().encode("utf-8")]),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=knowledge-policy-history.csv"},
            )
        return JSONResponse(payload)

    @api_router.post(
        "/rag/tenant-policy",
        summary="Update Tenant RAG Backend Policy",
        description="更新指定 tenant 的 RAG backend policy。已认证多租户请求不允许越权修改其他 tenant。",
        tags=["rag"],
    )
    async def api_rag_update_tenant_policy(req: RAGTenantPolicyRequest, request: Request):
        from zen_claw.auth.tenant import TenantStore
        from zen_claw.config.loader import get_data_dir

        try:
            resolved_tenant_id = _resolve_rag_tenant_id(request, str(req.tenant_id or ""))
            data_dir = get_data_dir()
            before = _read_tenant_policy(resolved_tenant_id)
            tenant = TenantStore(data_dir).set_store_backend(
                resolved_tenant_id, str(req.store_backend or "")
            )
            payload = {
                "ok": True,
                "tenant_id": tenant.tenant_id,
                "store_backend": str(tenant.store_backend or ""),
                "enabled": bool(tenant.enabled),
            }
            _append_knowledge_policy_audit(
                data_dir=data_dir,
                policy_kind="tenant",
                tenant_id=tenant.tenant_id,
                target_id=tenant.tenant_id,
                before_store_backend=str(before.get("store_backend") or ""),
                after_store_backend=str(tenant.store_backend or ""),
                actor=str(getattr(request.state, "username", "") or "api_key"),
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.post(
        "/rag/notebook-policy",
        summary="Update Notebook RAG Backend Policy",
        description="更新指定 tenant 下 notebook 的 RAG backend policy。不存在时会创建 notebook 元数据记录。",
        tags=["rag"],
    )
    async def api_rag_update_notebook_policy(req: RAGNotebookPolicyRequest, request: Request):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.knowledge.pipeline import RAGPipeline

        try:
            resolved_tenant_id = _resolve_rag_tenant_id(request, str(req.tenant_id or ""))
            data_dir = get_data_dir()
            before = _read_notebook_policy(resolved_tenant_id, str(req.notebook_id or ""))
            payload = RAGPipeline(
                data_dir,
                tenant_id=resolved_tenant_id,
            ).ensure_notebook(
                str(req.notebook_id or ""),
                store_backend=str(req.store_backend or ""),
            )
            payload["exists"] = True
            from zen_claw.auth.paths import tenant_data_dir
            from zen_claw.knowledge.notebook import NotebookManager

            notebook = NotebookManager(tenant_data_dir(data_dir, resolved_tenant_id)).set_retention_policy(
                str(req.notebook_id or ""),
                retention_max_documents=req.retention_max_documents,
                retention_max_age_days=req.retention_max_age_days,
            )
            payload["retention_max_documents"] = int(getattr(notebook, "retention_max_documents", 0) or 0)
            payload["retention_max_age_days"] = int(getattr(notebook, "retention_max_age_days", 0) or 0)
            _append_knowledge_policy_audit(
                data_dir=data_dir,
                policy_kind="notebook",
                tenant_id=resolved_tenant_id,
                target_id=str(payload.get("notebook_id") or ""),
                before_store_backend=str(before.get("store_backend") or ""),
                after_store_backend=str(payload.get("store_backend") or ""),
                actor=str(getattr(request.state, "username", "") or "api_key"),
                detail=(
                    f"retention docs: {int(before.get('retention_max_documents') or 0)} -> {int(payload.get('retention_max_documents') or 0)}; "
                    f"retention age: {int(before.get('retention_max_age_days') or 0)} -> {int(payload.get('retention_max_age_days') or 0)}"
                ),
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)

    @api_router.post(
        "/agent/invoke",
        summary="同步调用代理",
        description=(
            "向 AI 代理发送一条消息，等待完整回复后返回。\n\n"
            "- 需要 `X-API-Key` 请求头\n"
            "- 传入相同 `session_id` 可保持多轮对话上下文\n"
            "- 如需流式响应，请使用 `/agent/invoke/stream`"
        ),
        response_model=InvokeResponse,
        responses={
            400: {
                "description": "消息内容为空",
                "content": {"application/json": {"example": {"error": "message_required"}}},
            },
            401: {"description": "API Key 无效或缺失"},
            429: {"description": "请求过于频繁，已触发限流"},
        },
        tags=["agent"],
    )
    async def api_invoke(req: InvokeRequest):
        if not req.message.strip():
            return JSONResponse(status_code=400, content={"error": "message_required"})
        session_id = str(req.session_id or uuid.uuid4())
        text = await _invoke_agent_text(req.message, session_id)
        return InvokeResponse(response=text, session_id=session_id)

    @api_router.post(
        "/agent/invoke/stream",
        summary="流式调用代理 (SSE)",
        description=(
            "向 AI 代理发送消息，以 **Server-Sent Events (SSE)** 格式实时流式返回 token。\n\n"
            '每个事件格式为 `data: {"token": "..."}\\n\\n`，结束时发送 `data: [DONE]\\n\\n`。\n\n'
            "适用于需要实时打字机效果的前端场景。"
        ),
        responses={
            200: {"description": "SSE 流，Content-Type: text/event-stream"},
            400: {"description": "消息内容为空"},
            401: {"description": "API Key 无效或缺失"},
            429: {"description": "请求过于频繁，已触发限流"},
        },
        tags=["agent"],
    )
    async def api_invoke_stream(req: InvokeRequest):
        if not req.message.strip():
            return JSONResponse(status_code=400, content={"error": "message_required"})
        session_id = str(req.session_id or uuid.uuid4())
        text = await _invoke_agent_text(req.message, session_id)

        async def _gen():
            for ch in text:
                yield f"data: {json.dumps({'token': ch}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── A3: RBAC — Admin API Key Management ──────────────────────────────────

    @api_router.post(
        "/admin/api-keys",
        summary="创建 API Key（admin）",
        description="创建带角色的 API Key。只有 admin 级别的 Key 才能调用此接口。\n\nRole 可选值：`admin` / `operator` / `readonly`",
        tags=["admin"],
    )
    async def api_admin_create_key(request: Request):
        from zen_claw.auth.api_keys import ApiKeyStore

        # Require admin key to create new keys
        caller_key = str(request.headers.get("X-API-Key", ""))
        if caller_key:
            from zen_claw.config.loader import get_data_dir
            store = ApiKeyStore(get_data_dir())
            if not store.has_role(caller_key, "admin"):
                return JSONResponse(status_code=403, content={"error": "admin_required"})
        # else: if no API key system configured, allow creation (bootstrap)

        try:
            body = await request.json()
        except Exception:
            body = {}
        role = str(body.get("role") or "readonly")
        if role not in {"admin", "operator", "readonly"}:
            return JSONResponse(status_code=400, content={"error": "invalid_role", "valid": ["admin", "operator", "readonly"]})
        label = str(body.get("label") or "")

        from zen_claw.config.loader import get_data_dir
        store = ApiKeyStore(get_data_dir())
        raw, prefix = store.create(role=role, label=label)  # type: ignore[arg-type]
        return JSONResponse(status_code=201, content={"key": raw, "prefix": prefix, "role": role, "label": label})

    @api_router.get(
        "/admin/api-keys",
        summary="列出 API Keys（admin）",
        description="列出所有已创建的 API Key（不含原始 key 值）。只有 admin 级别才能查看。",
        tags=["admin"],
    )
    async def api_admin_list_keys(request: Request):
        from zen_claw.auth.api_keys import ApiKeyStore
        from zen_claw.config.loader import get_data_dir

        caller_key = str(request.headers.get("X-API-Key", ""))
        if caller_key:
            store = ApiKeyStore(get_data_dir())
            if not store.has_role(caller_key, "admin"):
                return JSONResponse(status_code=403, content={"error": "admin_required"})

        store = ApiKeyStore(get_data_dir())
        return {"keys": store.list_keys()}

    @api_router.delete(
        "/admin/api-keys/{prefix}",
        summary="撤销 API Key（admin）",
        description="按 Key 前缀禁用对应 API Key。",
        tags=["admin"],
    )
    async def api_admin_revoke_key(prefix: str, request: Request):
        from zen_claw.auth.api_keys import ApiKeyStore
        from zen_claw.config.loader import get_data_dir

        caller_key = str(request.headers.get("X-API-Key", ""))
        if caller_key:
            store = ApiKeyStore(get_data_dir())
            if not store.has_role(caller_key, "admin"):
                return JSONResponse(status_code=403, content={"error": "admin_required"})

        store = ApiKeyStore(get_data_dir())
        found = store.revoke(prefix)
        if not found:
            return JSONResponse(status_code=404, content={"error": "key_not_found"})
        return {"ok": True, "prefix": prefix}

    # ── A4: Conversation History ───────────────────────────────────────────────

    @api_router.get(
        "/conversations",
        summary="对话 Session 列表",
        description="分页列出所有对话 session，支持按 agent_id / channel / tenant_id 过滤。",
        tags=["conversations"],
    )
    async def api_conversations_list(
        agent_id: str = "",
        channel: str = "",
        tenant_id: str = "",
        limit: int = 20,
        offset: int = 0,
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.history.store import ConversationStore

        store = ConversationStore(get_data_dir())
        bounded = max(1, min(int(limit), 100))
        sessions, total = store.list_sessions(
            agent_id=agent_id, channel=channel, tenant_id=tenant_id,
            limit=bounded, offset=max(0, int(offset)),
        )
        return {
            "sessions": [
                {"id": s.id, "agent_id": s.agent_id, "channel": s.channel,
                 "tenant_id": s.tenant_id, "created_at": s.created_at}
                for s in sessions
            ],
            "total": total,
            "limit": bounded,
            "offset": offset,
        }

    @api_router.get(
        "/conversations/{session_id}",
        summary="获取完整对话",
        description="返回指定 session 的所有消息记录。",
        tags=["conversations"],
    )
    async def api_conversations_get(session_id: str):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.history.store import ConversationStore

        store = ConversationStore(get_data_dir())
        session = store.get_session(session_id)
        if not session:
            return JSONResponse(status_code=404, content={"error": "session_not_found"})
        messages = store.get_messages(session_id)
        return {
            "session": {"id": session.id, "agent_id": session.agent_id, "channel": session.channel,
                        "tenant_id": session.tenant_id, "created_at": session.created_at},
            "messages": [
                {"id": m.id, "role": m.role, "content": m.content, "tokens": m.tokens, "at_ms": m.at_ms}
                for m in messages
            ],
        }

    @api_router.get(
        "/conversations/{session_id}/export",
        summary="导出对话",
        description="导出对话为 JSON（默认）或 CSV（`?format=csv`）。",
        tags=["conversations"],
    )
    async def api_conversations_export(session_id: str, format: str = "json"):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.history.store import ConversationStore

        store = ConversationStore(get_data_dir())
        session = store.get_session(session_id)
        if not session:
            return JSONResponse(status_code=404, content={"error": "session_not_found"})
        if str(format).lower() == "csv":
            content = store.export_csv(session_id)
            return StreamingResponse(
                iter([content]),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{session_id}.csv"'},
            )
        return JSONResponse(content=json.loads(store.export_json(session_id)))

    # ── A1: Token Usage ───────────────────────────────────────────────────────

    @api_router.get(
        "/token-usage/summary",
        summary="Token 用量汇总",
        description="返回按 agent / model / day 分组的 token 消耗统计，支持时间范围过滤。",
        tags=["telemetry"],
    )
    async def api_token_usage_summary(
        since_ms: int = 0,
        until_ms: int = 0,
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.telemetry.token_usage import TokenUsageTracker

        tracker = TokenUsageTracker(get_data_dir())
        return tracker.summary(
            since_ms=since_ms or None,
            until_ms=until_ms or None,
        )

    @api_router.get(
        "/token-usage/records",
        summary="Token 用量记录列表",
        description="返回最近 N 条原始 token 用量记录（JSONL 格式）。",
        tags=["telemetry"],
    )
    async def api_token_usage_records(limit: int = 100):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.telemetry.token_usage import TokenUsageTracker

        tracker = TokenUsageTracker(get_data_dir())
        records = tracker.read_all(limit=max(1, min(int(limit), 1000)))
        return {"records": [r.to_dict() for r in records], "count": len(records)}

    @api_router.get(
        "/agent/workflow-path",
        summary="Agent 工作流路径",
        description="按 trace_id 或 session_id 重建 Agent 决策时间线，用于 Dashboard 路径可视化。",
        tags=["observability"],
    )
    async def api_agent_workflow_path(trace_id: str = "", session_id: str = ""):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.telemetry.workflow_path import WorkflowPathBuilder

        builder = WorkflowPathBuilder(get_data_dir())
        path = builder.build(trace_id=trace_id, session_id=session_id)
        return path.to_dict()

    @api_router.get(
        "/agent/workflow-recent",
        summary="最近 Agent 工作流",
        description="返回最近 N 条 trace 的工作流路径，用于 Dashboard 快速浏览。",
        tags=["observability"],
    )
    async def api_agent_workflow_recent(limit: int = 20):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.telemetry.workflow_path import WorkflowPathBuilder

        builder = WorkflowPathBuilder(get_data_dir())
        paths = builder.recent(limit=max(1, min(int(limit), 100)))
        return {"paths": [p.to_dict() for p in paths], "count": len(paths)}

    @api_router.get(
        "/token-usage/savings-report",
        summary="Token 节省报告",
        description="计算 cost_model 路由相对 base_model 的 token 成本节省。",
        tags=["telemetry"],
    )
    async def api_token_savings_report(
        base_model: str = "claude-opus-4-6",
        since_ms: int = 0,
    ):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.telemetry.savings import SavingsCalculator
        from zen_claw.telemetry.token_usage import TokenUsageTracker

        tracker = TokenUsageTracker(get_data_dir())
        calculator = SavingsCalculator(tracker, base_model=base_model)
        report = calculator.report(since_ms=int(since_ms) if since_ms else None)
        return report.to_dict()

    # ── B3: Webhook inbound trigger ───────────────────────────────────────────

    class AgentTriggerRequest(BaseModel):
        agent_id: str
        message: str
        session_id: str = ""
        extra: dict = {}

    @api_router.post(
        "/agent/trigger",
        summary="外部触发 Agent 任务",
        description="外部系统通过此端点触发一次 Agent 任务。返回 trigger_id 供后续查询。",
        tags=["webhooks"],
    )
    async def api_agent_trigger(body: AgentTriggerRequest):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.webhooks.outbound import WebhookDispatcher

        trigger_id = uuid.uuid4().hex[:16]
        dispatcher = WebhookDispatcher(get_data_dir())
        # Log the inbound trigger as an audit record (no outbound dispatch here)
        dispatcher._append_log(  # noqa: SLF001
            __import__("zen_claw.webhooks.outbound", fromlist=["WebhookDispatchRecord"]).WebhookDispatchRecord(
                at_ms=int(time.time() * 1000),
                url="inbound",
                agent_id=body.agent_id,
                session_id=body.session_id,
                intent="trigger",
                status_code=200,
                attempts=1,
                success=True,
                error="",
            )
        )
        return {
            "trigger_id": trigger_id,
            "agent_id": body.agent_id,
            "session_id": body.session_id,
            "queued": True,
        }

    @api_router.get(
        "/agent/webhook-log",
        summary="Webhook 派发审计日志",
        description="返回最近 N 条 webhook 派发记录。",
        tags=["webhooks"],
    )
    async def api_webhook_log(limit: int = 100):
        from zen_claw.config.loader import get_data_dir
        from zen_claw.webhooks.outbound import WebhookDispatcher

        dispatcher = WebhookDispatcher(get_data_dir())
        records = dispatcher.read_log(limit=max(1, min(int(limit), 1000)))
        return {"records": [r.to_dict() for r in records], "count": len(records)}

    api_app.include_router(api_router)
else:
    api_app = None


def trigger_cron_job_with_audit(job_id: str, *, data_dir: Path) -> dict[str, Any]:
    """Trigger one cron job and append a dashboard audit event."""
    from zen_claw.cron.service import CronService

    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    cron_store = data_dir / "cron" / "jobs.json"
    service = CronService(cron_store)
    ok = asyncio.run(service.run_job(job_id, force=True))

    dashboard_dir = data_dir / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    audit_file = dashboard_dir / "audit.log.jsonl"
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "dashboard.cron.run",
        "job_id": job_id,
        "ok": bool(ok),
    }
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return {"ok": bool(ok), "job_id": job_id, "trace_id": trace_id}


def _append_knowledge_policy_audit(
    *,
    data_dir: Path,
    policy_kind: str,
    tenant_id: str,
    target_id: str,
    before_store_backend: str,
    after_store_backend: str,
    actor: str,
    detail: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "knowledge.policy.update",
        "policy_kind": str(policy_kind),
        "tenant_id": str(tenant_id),
        "target_id": str(target_id),
        "before_store_backend": str(before_store_backend or ""),
        "after_store_backend": str(after_store_backend or ""),
        "actor": str(actor or "api_key"),
        "detail": str(detail or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "knowledge_policy.log.jsonl", event)
    return event


def _read_knowledge_policy_history(
    *,
    data_dir: Path,
    policy_kind: str = "",
    actor: str = "",
    tenant_id: str = "",
    target_id: str = "",
    before_store_backend: str = "",
    after_store_backend: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    kind_filter = str(policy_kind or "").strip().lower()
    actor_filter = str(actor or "").strip().lower()
    tenant_filter = str(tenant_id or "").strip().lower()
    target_filter = str(target_id or "").strip().lower()
    before_backend_filter = str(before_store_backend or "").strip().lower()
    after_backend_filter = str(after_store_backend or "").strip().lower()
    rows = [
        row
        for row in _read_jsonl(data_dir / "dashboard" / "knowledge_policy.log.jsonl", limit=200)
        if isinstance(row, dict)
    ]
    rows = sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    filtered = [
        row
        for row in rows
        if (not kind_filter or str(row.get("policy_kind") or "").strip().lower() == kind_filter)
        and (not actor_filter or str(row.get("actor") or "").strip().lower() == actor_filter)
        and (not tenant_filter or str(row.get("tenant_id") or "").strip().lower() == tenant_filter)
        and (not target_filter or str(row.get("target_id") or "").strip().lower() == target_filter)
        and (
            not before_backend_filter
            or str(row.get("before_store_backend") or "").strip().lower() == before_backend_filter
        )
        and (
            not after_backend_filter
            or str(row.get("after_store_backend") or "").strip().lower() == after_backend_filter
        )
    ]
    safe_limit = max(1, min(100, int(limit or 20)))
    safe_offset = max(0, int(offset or 0))
    page = filtered[safe_offset : safe_offset + safe_limit]
    return {
        "ok": True,
        "policy_kind": kind_filter,
        "actor": actor_filter,
        "tenant_id": tenant_filter,
        "target_id": target_filter,
        "before_store_backend": before_backend_filter,
        "after_store_backend": after_backend_filter,
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(page),
        "total": len(filtered),
        "rows": page,
    }


def _read_knowledge_activity_history(
    *,
    data_dir: Path,
    event_kind: str = "",
    tenant_id: str = "",
    actor: str = "",
    status: str = "",
    notebook_id: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    kind_filter = str(event_kind or "").strip().lower()
    tenant_filter = str(tenant_id or "").strip().lower()
    actor_filter = str(actor or "").strip().lower()
    status_filter = str(status or "").strip().lower()
    notebook_filter = str(notebook_id or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for row in _read_jsonl(data_dir / "dashboard" / "knowledge_policy.log.jsonl", limit=200):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "at_ms": int(row.get("at_ms") or 0),
                "event_kind": "policy",
                "tenant_id": str(row.get("tenant_id") or ""),
                "target_id": str(row.get("target_id") or ""),
                "actor": str(row.get("actor") or ""),
                "status": "ok",
                "detail": (
                    f"{str(row.get('policy_kind') or 'policy')}: "
                    f"{str(row.get('before_store_backend') or 'default')} -> "
                    f"{str(row.get('after_store_backend') or 'default')}"
                ),
                "trace_id": str(row.get("trace_id") or ""),
            }
        )
    for row in _read_jsonl(data_dir / "dashboard" / "knowledge_cron.log.jsonl", limit=200):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "at_ms": int(row.get("at_ms") or 0),
                "event_kind": "cron",
                "tenant_id": str(row.get("tenant_id") or ""),
                "target_id": str(row.get("knowledge_notebook") or row.get("notebook_id") or ""),
                "actor": str(row.get("actor") or "system"),
                "status": str(row.get("status") or ""),
                "detail": (
                    f"{str(row.get('job_name') or 'cron')}: "
                    f"chunks={int(row.get('chunks_added') or 0)}; docs={int(row.get('documents') or 0)}"
                ),
                "trace_id": str(row.get("trace_id") or ""),
            }
        )
    rows = sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    filtered = [
        row
        for row in rows
        if (not kind_filter or str(row.get("event_kind") or "").strip().lower() == kind_filter)
        and (not tenant_filter or str(row.get("tenant_id") or "").strip().lower() == tenant_filter)
        and (not actor_filter or str(row.get("actor") or "").strip().lower() == actor_filter)
        and (not status_filter or str(row.get("status") or "").strip().lower() == status_filter)
        and (not notebook_filter or str(row.get("target_id") or "").strip().lower() == notebook_filter)
    ]
    safe_limit = max(1, min(100, int(limit or 20)))
    safe_offset = max(0, int(offset or 0))
    page = filtered[safe_offset : safe_offset + safe_limit]
    return {
        "ok": True,
        "event_kind": kind_filter,
        "tenant_id": tenant_filter,
        "actor": actor_filter,
        "status": status_filter,
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(page),
        "total": len(filtered),
        "rows": page,
    }


def _read_agent_action_history(
    *,
    data_dir: Path,
    agent_id: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    agent_filter = str(agent_id or "").strip().lower()
    action_filter = str(action or "").strip().lower()
    actor_filter = str(actor or "").strip().lower()
    rows = [
        row
        for row in _read_jsonl(data_dir / "dashboard" / "agent_actions.log.jsonl", limit=200)
        if isinstance(row, dict)
    ]
    rows = sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    filtered = [
        row
        for row in rows
        if (not agent_filter or str(row.get("agent_id") or "").strip().lower() == agent_filter)
        and (not action_filter or str(row.get("action") or "").strip().lower() == action_filter)
        and (not actor_filter or str(row.get("actor") or "").strip().lower() == actor_filter)
    ]
    safe_limit = max(1, min(100, int(limit or 20)))
    safe_offset = max(0, int(offset or 0))
    page = filtered[safe_offset : safe_offset + safe_limit]
    return {
        "ok": True,
        "agent_id": agent_filter,
        "action": action_filter,
        "actor": actor_filter,
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(page),
        "total": len(filtered),
        "rows": page,
    }


def _read_channel_action_history(
    *,
    data_dir: Path,
    channel_name: str = "",
    action: str = "",
    actor: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    channel_filter = str(channel_name or "").strip().lower()
    action_filter = str(action or "").strip().lower()
    actor_filter = str(actor or "").strip().lower()
    rows = [
        row
        for row in _read_jsonl(data_dir / "dashboard" / "channel_actions.log.jsonl", limit=200)
        if isinstance(row, dict)
    ]
    rows = sorted(rows, key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    filtered = [
        row
        for row in rows
        if (not channel_filter or str(row.get("channel_name") or "").strip().lower() == channel_filter)
        and (not action_filter or str(row.get("action") or "").strip().lower() == action_filter)
        and (not actor_filter or str(row.get("actor") or "").strip().lower() == actor_filter)
    ]
    safe_limit = max(1, min(100, int(limit or 20)))
    safe_offset = max(0, int(offset or 0))
    page = filtered[safe_offset : safe_offset + safe_limit]
    return {
        "ok": True,
        "channel_name": channel_filter,
        "action": action_filter,
        "actor": actor_filter,
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(page),
        "total": len(filtered),
        "rows": page,
    }


def _append_skill_action_audit(
    *,
    data_dir: Path,
    skill_name: str,
    action: str,
    enabled: bool,
    actor: str,
    detail: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "skills.state.change",
        "skill_name": str(skill_name),
        "action": str(action),
        "enabled": bool(enabled),
        "actor": str(actor or "api_key"),
        "detail": str(detail or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "skills_actions.log.jsonl", event)
    return event


def _append_skill_check_audit(
    *,
    data_dir: Path,
    payload: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    event = {
        "at_ms": int(time.time() * 1000),
        "trace_id": uuid.uuid4().hex[:12],
        "event": "skills.preflight",
        "skill_name": str(payload.get("name") or ""),
        "ok": bool(payload.get("ok")),
        "manifest_ok": bool(payload.get("manifest_ok")),
        "integrity_ok": bool(payload.get("integrity_ok")),
        "tests_present": bool(payload.get("tests_present")),
        "manifest_errors": list(payload.get("manifest_errors") or [])[:5],
        "integrity_errors": list(payload.get("integrity_errors") or [])[:5],
        "detail": str(payload.get("detail") or ""),
        "actor": str(actor or "api_key"),
    }
    _append_jsonl(data_dir / "dashboard" / "skills_checks.log.jsonl", event)
    return event


def _append_skill_export_audit(
    *,
    data_dir: Path,
    skill_name: str,
    out_path: Path,
    message: str,
    actor: str,
    exported_physical_dir: str = "",
    exported_version: str = "",
    export_hash: str = "",
) -> dict[str, Any]:
    event = {
        "at_ms": int(time.time() * 1000),
        "trace_id": uuid.uuid4().hex[:12],
        "event": "skills.export",
        "skill_name": str(skill_name or "").strip(),
        "out_path": str(out_path),
        "message": str(message or ""),
        "actor": str(actor or "api_key"),
        "exported_physical_dir": str(exported_physical_dir or "").strip(),
        "exported_version": str(exported_version or "").strip(),
        "export_hash": str(export_hash or "").strip(),
    }
    _append_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", event)
    return {
        "ok": True,
        "skill_name": event["skill_name"],
        "out_path": event["out_path"],
        "message": event["message"],
        "exported_physical_dir": event["exported_physical_dir"],
        "exported_version": event["exported_version"],
        "export_hash": event["export_hash"],
        "event": event,
    }


def _latest_skill_export_event(*, data_dir: Path, skill_name: str) -> dict[str, Any]:
    rows = sorted(
        [
            item
            for item in _read_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", limit=100)
            if isinstance(item, dict)
            and str(item.get("skill_name") or "").strip() == str(skill_name or "").strip()
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    return rows[0] if rows else {}


def _latest_skill_export_path(*, data_dir: Path, skill_name: str) -> Path | None:
    rows = sorted(
        [
            item
            for item in _read_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", limit=100)
            if isinstance(item, dict) and str(item.get("skill_name") or "").strip() == str(skill_name or "").strip()
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )
    for row in rows:
        out_path = Path(str(row.get("out_path") or "").strip())
        if out_path.exists() and out_path.is_file():
            return out_path
    return None


def _skill_package_policy_path(*, data_dir: Path) -> Path:
    return data_dir / "dashboard" / "skills_package_policy.json"


def _load_skill_package_policies(*, data_dir: Path) -> dict[str, dict[str, Any]]:
    path = _skill_package_policy_path(data_dir=data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        rows[str(key).strip()] = dict(value)
    return rows


def _save_skill_package_policies(*, data_dir: Path, policies: dict[str, dict[str, Any]]) -> None:
    path = _skill_package_policy_path(data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(policies, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _build_skill_package_policy(
    *, data_dir: Path, skill_name: str, package_state: dict[str, Any]
) -> dict[str, Any]:
    policies = _load_skill_package_policies(data_dir=data_dir)
    stored = dict(policies.get(skill_name, {}))
    preferred_physical_dir = str(stored.get("preferred_physical_dir") or "").strip()
    preferred_version = str(stored.get("preferred_version") or "").strip()
    retention_hours = int(stored.get("retention_hours") or 24)
    require_export_for_restore = bool(stored.get("require_export_for_restore", True))
    package_rows = list(package_state.get("package_rows") or [])
    latest_export_path = str(package_state.get("latest_export_path") or "").strip()
    available_dirs = {
        str(row.get("physical_dir") or "").strip(): dict(row)
        for row in package_rows
        if isinstance(row, dict)
    }
    available_versions = {
        str(row.get("version") or "").strip()
        for row in package_rows
        if isinstance(row, dict) and str(row.get("version") or "").strip()
    }
    warnings: list[str] = []
    if preferred_physical_dir and preferred_physical_dir not in available_dirs:
        warnings.append(f"preferred physical dir missing: {preferred_physical_dir}")
    if preferred_version and preferred_version not in available_versions:
        warnings.append(f"preferred version missing: {preferred_version}")
    if require_export_for_restore and not latest_export_path:
        warnings.append("restore requires export but no exported package is available")
    return {
        "preferred_physical_dir": preferred_physical_dir,
        "preferred_version": preferred_version,
        "retention_hours": retention_hours,
        "require_export_for_restore": require_export_for_restore,
        "restore_ready": bool(latest_export_path) or not require_export_for_restore,
        "available_physical_dirs": sorted(available_dirs.keys()),
        "available_versions": sorted(available_versions),
        "warnings": warnings,
    }


def _build_skill_restore_plan(
    *, data_dir: Path, skill_name: str, package_state: dict[str, Any], package_policy: dict[str, Any]
) -> dict[str, Any]:
    latest_export_path = str(package_state.get("latest_export_path") or "").strip()
    latest_export_event = _latest_skill_export_event(data_dir=data_dir, skill_name=skill_name)
    export_manifest = _read_skill_export_manifest(Path(latest_export_path)) if latest_export_path else {}
    export_version = str(
        latest_export_event.get("exported_version") or (export_manifest or {}).get("version") or ""
    ).strip()
    export_physical_dir = str(latest_export_event.get("exported_physical_dir") or "").strip()
    selected_dir = str(package_state.get("selected_physical_dir") or "").strip()
    selected_row = None
    for row in list(package_state.get("package_rows") or []):
        if isinstance(row, dict) and str(row.get("physical_dir") or "").strip() == selected_dir:
            selected_row = row
            break
    selected_version = str((selected_row or {}).get("version") or "").strip()
    preferred_dir = str(package_policy.get("preferred_physical_dir") or "").strip()
    preferred_version = str(package_policy.get("preferred_version") or "").strip()
    warnings = list(package_policy.get("warnings") or [])
    blocked_reasons: list[str] = []
    if not latest_export_path:
        blocked_reasons.append("no exported package is available")
    if preferred_version and export_version and preferred_version != export_version:
        warnings.append(f"export package version differs from preferred version: {export_version}")
        blocked_reasons.append(
            f"export package version does not match preferred version: {preferred_version}"
        )
    if preferred_dir and export_physical_dir and preferred_dir != export_physical_dir:
        warnings.append(f"export package differs from preferred dir: {export_physical_dir}")
        blocked_reasons.append(f"export package dir does not match preferred dir: {preferred_dir}")
    if preferred_dir and selected_dir and preferred_dir != selected_dir:
        warnings.append(f"selected package differs from preferred dir: {selected_dir}")
        if not export_physical_dir:
            blocked_reasons.append(f"selected package dir does not match preferred dir: {preferred_dir}")
    if preferred_version and selected_version and preferred_version != selected_version:
        warnings.append(f"selected package differs from preferred version: {selected_version}")
        blocked_reasons.append(
            f"selected package version does not match preferred version: {preferred_version}"
        )

    # ------------------------------------------------------------------
    # Hash verification: compare stored export_hash against current zip.
    # Skip silently when no hash was recorded (backward-compatible with
    # exports created before this field was added).
    # ------------------------------------------------------------------
    stored_hash = str(latest_export_event.get("export_hash") or "").strip()
    current_zip_hash = ""
    hash_verified = True  # default True when no stored hash (no-op)
    if stored_hash and latest_export_path:
        try:
            current_zip_hash = hashlib.sha256(
                Path(latest_export_path).read_bytes()
            ).hexdigest()
            hash_verified = current_zip_hash == stored_hash
        except OSError:
            current_zip_hash = ""
            hash_verified = False
    if stored_hash and not hash_verified:
        blocked_reasons.append(
            f"export zip hash mismatch: expected {stored_hash[:12]}... "
            f"got {current_zip_hash[:12] if current_zip_hash else 'unreadable'}..."
        )

    return {
        "skill_name": skill_name,
        "source": "latest_export",
        "latest_export_path": latest_export_path,
        "export_physical_dir": export_physical_dir,
        "export_version": export_version,
        "selected_physical_dir": selected_dir,
        "selected_version": selected_version,
        "preferred_physical_dir": preferred_dir,
        "preferred_version": preferred_version,
        "export_hash": stored_hash,
        "current_zip_hash": current_zip_hash,
        "hash_verified": hash_verified,
        "warnings": warnings,
        "blocked_reasons": blocked_reasons,
        "ready": not blocked_reasons,
    }


def _read_skill_export_manifest(export_path: Path) -> dict[str, Any]:
    if not export_path.exists() or not export_path.is_file():
        return {}
    try:
        with zipfile.ZipFile(export_path, "r") as zf:
            manifest_members = [
                name
                for name in zf.namelist()
                if str(name or "").endswith("/manifest.json") or str(name or "") == "manifest.json"
            ]
            if len(manifest_members) != 1:
                return {}
            with zf.open(manifest_members[0], "r") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _update_skill_package_policy(
    *,
    name: str,
    preferred_physical_dir: str | None,
    preferred_version: str | None,
    retention_hours: int | None,
    require_export_for_restore: bool | None,
) -> JSONResponse:
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir, load_config

    cfg = load_config()
    data_dir = get_data_dir()
    loader = SkillsLoader(cfg.workspace_path)
    skill_name = str(name or "").strip()
    inventory_rows = {row["name"]: row for row in loader.list_skills(filter_unavailable=False)}
    if skill_name not in inventory_rows:
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")
    package_state = _read_skill_package_state(loader=loader, skill_name=skill_name, data_dir=data_dir)
    package_rows = [row for row in list(package_state.get("package_rows") or []) if isinstance(row, dict)]
    normalized_dir = str(preferred_physical_dir or "").strip()
    normalized_version = str(preferred_version or "").strip()
    if normalized_dir and normalized_dir not in {
        str(row.get("physical_dir") or "").strip() for row in package_rows
    }:
        raise HTTPException(status_code=400, detail=f"unknown skill package dir: {normalized_dir}")
    if normalized_version and normalized_version not in {
        str(row.get("version") or "").strip() for row in package_rows if str(row.get("version") or "").strip()
    }:
        raise HTTPException(status_code=400, detail=f"unknown skill package version: {normalized_version}")
    policies = _load_skill_package_policies(data_dir=data_dir)
    stored = dict(policies.get(skill_name, {}))
    if preferred_physical_dir is not None:
        stored["preferred_physical_dir"] = normalized_dir
    if preferred_version is not None:
        stored["preferred_version"] = normalized_version
    if retention_hours is not None:
        stored["retention_hours"] = int(retention_hours)
    if require_export_for_restore is not None:
        stored["require_export_for_restore"] = bool(require_export_for_restore)
    policies[skill_name] = stored
    _save_skill_package_policies(data_dir=data_dir, policies=policies)
    policy = _build_skill_package_policy(
        data_dir=data_dir,
        skill_name=skill_name,
        package_state=package_state,
    )
    event = _append_skill_action_audit(
        data_dir=data_dir,
        skill_name=skill_name,
        action="package_policy",
        enabled=bool(inventory_rows[skill_name].get("enabled")),
        actor="api_key",
        detail=(
            f"preferred_dir={policy['preferred_physical_dir'] or '-'}; "
            f"preferred_version={policy['preferred_version'] or '-'}; "
            f"retention_hours={int(policy['retention_hours'] or 0)}; "
            f"require_export_for_restore={bool(policy['require_export_for_restore'])}"
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "name": skill_name,
            "package_policy": policy,
            "event": event,
            "reload_required": False,
            "apply_state": _build_apply_state(
                surface="skill",
                target=skill_name,
                reload_required=False,
                execution_mode="package_policy",
            ),
        }
    )


def _read_skill_restore_plan(*, name: str) -> JSONResponse:
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir, load_config

    cfg = load_config()
    data_dir = get_data_dir()
    loader = SkillsLoader(cfg.workspace_path)
    skill_name = str(name or "").strip()
    package_state = _read_skill_package_state(loader=loader, skill_name=skill_name, data_dir=data_dir)
    package_policy = _build_skill_package_policy(
        data_dir=data_dir,
        skill_name=skill_name,
        package_state=package_state,
    )
    restore_plan = _build_skill_restore_plan(
        data_dir=data_dir,
        skill_name=skill_name,
        package_state=package_state,
        package_policy=package_policy,
    )
    if (
        not restore_plan["latest_export_path"]
        and not list(package_state.get("package_rows") or [])
        and not str(package_state.get("selected_physical_dir") or "").strip()
    ):
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")
    return JSONResponse(
        {
            "ok": True,
            "name": skill_name,
            "restore_plan": restore_plan,
            "package_policy": package_policy,
        }
    )


def _restore_skill_with_policy(*, name: str) -> JSONResponse:
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir, load_config

    cfg = load_config()
    data_dir = get_data_dir()
    loader = SkillsLoader(cfg.workspace_path)
    skill_name = str(name or "").strip()
    package_state = _read_skill_package_state(loader=loader, skill_name=skill_name, data_dir=data_dir)
    package_policy = _build_skill_package_policy(
        data_dir=data_dir, skill_name=skill_name, package_state=package_state
    )
    restore_plan = _build_skill_restore_plan(
        data_dir=data_dir,
        skill_name=skill_name,
        package_state=package_state,
        package_policy=package_policy,
    )
    export_path = _latest_skill_export_path(data_dir=data_dir, skill_name=skill_name)
    if (
        export_path is None
        and not list(package_state.get("package_rows") or [])
        and not str(package_state.get("selected_physical_dir") or "").strip()
    ):
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")
    if export_path is None or not restore_plan["ready"]:
        raise HTTPException(
            status_code=409,
            detail="skill restore blocked: "
            + "; ".join(
                [str(item or "").strip() for item in list(restore_plan.get("blocked_reasons") or []) if str(item or "").strip()]
                or ["missing export or package policy restriction"]
            ),
        )
    ok, msg = loader.install_skill_from_zip(
        export_path,
        name=skill_name,
        overwrite=True,
        require_manifest=False,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    # Post-install manifest integrity check.
    try:
        integrity_ok, integrity_errors = loader.verify_skill_integrity(skill_name)
    except Exception:
        integrity_ok, integrity_errors = False, ["integrity check raised an exception"]

    event = _append_skill_action_audit(
        data_dir=data_dir,
        skill_name=skill_name,
        action="restore",
        enabled=True,
        actor="api_key",
        detail=f"{msg} | source={export_path} | integrity={'ok' if integrity_ok else 'warn'}",
    )
    return JSONResponse(
        {
            "ok": True,
            "name": skill_name,
            "source": str(export_path),
            "message": msg,
            "restore_plan": restore_plan,
            "package_policy": package_policy,
            "integrity_check": {
                "ok": integrity_ok,
                "errors": integrity_errors,
            },
            "event": event,
            "reload_required": False,
            "apply_state": _build_apply_state(
                surface="skill",
                target=skill_name,
                reload_required=False,
                execution_mode="restore",
            ),
        }
    )


def _read_skill_package_state(*, loader, skill_name: str, data_dir: Path) -> dict[str, Any]:
    selected_path = loader.resolve_physical_path(skill_name)
    selected_dir = selected_path.name if selected_path else ""
    mapped_dirs = list(loader._skill_mapping.get(skill_name, []))
    package_rows: list[dict[str, Any]] = []
    for physical_dir in mapped_dirs:
        path = loader.workspace_skills / physical_dir
        source = "workspace"
        if not path.exists() and loader.builtin_skills:
            builtin_path = loader.builtin_skills / physical_dir
            if builtin_path.exists():
                path = builtin_path
                source = "builtin"
        manifest_version = ""
        manifest_path = path / "manifest.json"
        if manifest_path.exists():
            try:
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_version = str(manifest_payload.get("version") or "").strip()
            except Exception:
                manifest_version = ""
        package_rows.append(
            {
                "physical_dir": physical_dir,
                "path": str(path),
                "source": source,
                "version": manifest_version,
                "exists": bool(path.exists()),
                "selected": physical_dir == selected_dir,
                "versioned": "_v" in physical_dir,
            }
        )
    journal_entries = [
        item
        for item in list(loader._load_journal() or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip() == skill_name
    ]
    latest_export_path = _latest_skill_export_path(data_dir=data_dir, skill_name=skill_name)
    return {
        "selected_physical_dir": selected_dir,
        "mapped_directories": mapped_dirs,
        "package_rows": package_rows,
        "versioned_package_count": len([row for row in package_rows if bool(row.get("versioned"))]),
        "journal_entries": journal_entries,
        "journal_pending_count": len(journal_entries),
        "latest_export_path": str(latest_export_path) if latest_export_path else "",
        "versioned_layout_enabled": str(os.environ.get("ZEN_CLAW_ENABLE_VERSIONED_SKILL_DIRS", "")).strip().lower()
        in {"1", "true", "yes", "on"},
    }


def _promote_skill(*, name: str, declarative_intent: str, note: str | None = None) -> JSONResponse:
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir, load_config

    cfg = load_config()
    data_dir = get_data_dir()
    loader = SkillsLoader(cfg.workspace_path)
    skill_name = str(name or "").strip()
    target_intent = str(declarative_intent or "").strip()
    if not target_intent:
        raise HTTPException(status_code=400, detail="declarative_intent is required")
    ok, msg = loader.update_skill_governance_metadata(
        skill_name,
        promote_status="promoted",
        promote_target_intent=target_intent,
        promoted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    detail = (
        f"manual promote recorded: declarative_intent={target_intent}; "
        f"next_step=handwrite DeclarativeIntent and register into Gate 1"
    )
    if str(note or "").strip():
        detail += f"; note={str(note).strip()}"
    event = _append_skill_action_audit(
        data_dir=data_dir,
        skill_name=skill_name,
        action="promote",
        enabled=loader.is_skill_enabled(skill_name),
        actor="api_key",
        detail=detail,
    )
    return JSONResponse(
        {
            "ok": True,
            "name": skill_name,
            "declarative_intent": target_intent,
            "message": msg,
            "event": event,
            "reload_required": False,
            "apply_state": _build_apply_state(
                surface="skill",
                target=skill_name,
                reload_required=False,
                execution_mode="promote",
            ),
        }
    )


def _preview_skill_gc(*, loader, retention_hours: int) -> dict[str, Any]:
    cutoff = datetime.now().timestamp() - (max(0, int(retention_hours)) * 3600)
    pins = set()
    sessions_dir = Path.home() / ".zen-claw" / "sessions"
    if sessions_dir.exists():
        for path in sessions_dir.glob("*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    for _ in range(5):
                        line = f.readline()
                        if not line:
                            break
                        data = json.loads(line)
                        if data.get("_type") == "metadata":
                            meta = data.get("metadata", {})
                            for pin in meta.get("skill_pins", {}).values():
                                pins.add(str(pin))
                            break
            except Exception:
                continue
    for physical_dirs in loader._skill_mapping.values():
        for physical in physical_dirs:
            pins.add(str(physical))

    candidates: list[dict[str, Any]] = []
    if loader.workspace_skills.exists():
        for item in loader.workspace_skills.iterdir():
            if not item.is_dir():
                continue
            if item.name in pins or "_v" not in item.name:
                continue
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                candidates.append(
                    {
                        "physical_dir": item.name,
                        "path": str(item),
                        "modified_at_ms": int(mtime * 1000),
                        "age_hours": round(max(0.0, (datetime.now().timestamp() - mtime) / 3600), 2),
                    }
                )
    candidates.sort(key=lambda row: (str(row.get("physical_dir") or "").lower(), str(row.get("path") or "")))
    return {
        "ok": True,
        "retention_hours": int(retention_hours),
        "candidate_count": len(candidates),
        "candidates": candidates[:20],
    }


def _append_agent_action_audit(
    *,
    data_dir: Path,
    agent_id: str,
    action: str,
    planning_enabled: bool,
    actor: str,
    detail: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "agent.profile.updated",
        "agent_id": str(agent_id or "").strip() or "default",
        "action": str(action),
        "planning_enabled": bool(planning_enabled),
        "actor": str(actor or "api_key"),
        "detail": str(detail or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "agent_actions.log.jsonl", event)
    return event


def _build_reload_execute_plan(
    cfg: Any,
    *,
    surface: str = "",
    target: str = "",
) -> list[dict[str, Any]]:
    """Build a per-surface execution plan for reload-execute.

    execution_mode:
      config_reload  — reload in-memory config (agent/skill/rag surfaces)
      in_process     — attempt true in-process restart (channel.webchat)
      audit_only     — record but don't execute (external channels)
      unsupported    — unknown surface
    """
    surface_filter = str(surface or "").strip().lower()
    entries: list[dict[str, Any]] = [
        {
            "surface": "agent",
            "target": target or "default",
            "executable": True,
            "execution_mode": "config_reload",
            "status": "pending",
        },
        {
            "surface": "channel.webchat",
            "target": "webchat",
            "executable": True,
            "execution_mode": "in_process",
            "status": "pending",
        },
    ]
    # Add any non-webchat channels from config as audit_only
    try:
        channels = dict(getattr(cfg, "channels", {}) or {})
        for ch_name in channels:
            if str(ch_name or "").strip().lower() not in ("webchat", ""):
                entries.append({
                    "surface": f"channel.{ch_name}",
                    "target": str(ch_name),
                    "executable": True,
                    "execution_mode": "audit_only",
                    "status": "pending",
                })
    except Exception:  # noqa: BLE001
        pass

    if surface_filter:
        entries = [e for e in entries if str(e.get("surface") or "").lower() == surface_filter]
    return entries


def _append_reload_execute_audit(
    *,
    data_dir: Path,
    surface: str,
    target: str,
    execution_mode: str,
    ok: bool,
    error: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    event = {
        "at_ms": now_ms,
        "trace_id": uuid.uuid4().hex[:12],
        "event_type": "reload_execute",
        "surface": str(surface or ""),
        "target": str(target or ""),
        "execution_mode": str(execution_mode or ""),
        "ok": bool(ok),
        "error": str(error or ""),
        "dry_run": bool(dry_run),
    }
    _append_jsonl(data_dir / "dashboard" / "reload_execute.log.jsonl", event)
    return event


def _append_agent_apply_audit(
    *,
    data_dir: Path,
    actor: str,
    note: str,
    target: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "agent.config.applied",
        "actor": str(actor or "api_key"),
        "note": str(note or "applied"),
        "target": str(target or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "agent_apply.log.jsonl", event)
    return event


def _run_skill_preflight(*, loader, name: str) -> dict[str, Any]:
    from pathlib import Path as _Path

    skill_name = str(name or "").strip()
    skill_rows = {row["name"]: row for row in loader.list_skills(filter_unavailable=False)}
    row = skill_rows.get(skill_name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")
    manifest_ok, manifest_errors = loader.validate_skill_manifest(skill_name, strict=True)
    integrity_ok, integrity_errors = loader.verify_skill_integrity(
        skill_name, require_integrity=False
    )
    tests_present = _Path(str(row.get("path") or "")).parent.joinpath("tests").is_dir()
    detail_parts: list[str] = []
    if not manifest_ok:
        detail_parts.append(
            "manifest: " + "; ".join(str(item) for item in list(manifest_errors or [])[:2])
        )
    if not integrity_ok:
        detail_parts.append(
            "integrity: " + "; ".join(str(item) for item in list(integrity_errors or [])[:2])
        )
    if not detail_parts:
        detail_parts.append("manifest/integrity checks passed")
    return {
        "name": skill_name,
        "ok": bool(manifest_ok and integrity_ok),
        "manifest_ok": bool(manifest_ok),
        "manifest_errors": list(manifest_errors or []),
        "integrity_ok": bool(integrity_ok),
        "integrity_errors": list(integrity_errors or []),
        "tests_present": bool(tests_present),
        "enabled": bool(loader.is_skill_enabled(skill_name)),
        "detail": " | ".join(detail_parts),
    }


def _run_skill_preflight_batch(
    *,
    loader,
    names: list[str] | None = None,
    include_disabled: bool = True,
) -> dict[str, Any]:
    inventory = sorted(loader.list_skills(filter_unavailable=False), key=lambda row: row["name"])
    requested_names = [str(item or "").strip() for item in list(names or []) if str(item or "").strip()]
    requested_set = set(requested_names)
    rows: list[dict[str, Any]] = []
    missing_names = sorted(name for name in requested_set if name not in {row["name"] for row in inventory})
    for row in inventory:
        skill_name = str(row.get("name") or "").strip()
        if not skill_name:
            continue
        if requested_set and skill_name not in requested_set:
            continue
        if not include_disabled and not bool(loader.is_skill_enabled(skill_name)):
            continue
        rows.append(_run_skill_preflight(loader=loader, name=skill_name))
    return {
        "rows": rows,
        "requested_names": requested_names,
        "missing_names": missing_names,
        "include_disabled": bool(include_disabled),
        "checked_count": len(rows),
        "ok_count": len([row for row in rows if bool(row.get("ok"))]),
        "issue_count": len([row for row in rows if not bool(row.get("ok"))]),
    }


def _build_skill_health_summary(
    rows: list[dict[str, Any]],
    *,
    latest_checks: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return aggregated skill health metrics from inventory rows and latest preflight checks."""
    total = len(rows)

    def pct(n: int) -> float:
        return round(n / total * 100, 1) if total > 0 else 0.0

    enabled = sum(1 for r in rows if r.get("enabled"))
    manifest_ok = sum(1 for r in rows if str(r.get("manifest") or "") == "valid")
    trusted = sum(1 for r in rows if str(r.get("trust") or "") == "trusted")
    runtime_covered = sum(
        1 for r in rows if str(r.get("runtime_intent_mode") or "").strip() not in ("", "none")
    )
    tests_present = sum(1 for r in rows if r.get("tests_present"))

    preflight_ok = 0
    preflight_issues = 0
    if latest_checks:
        for _name, check in latest_checks.items():
            if check.get("ok") is True:
                preflight_ok += 1
            elif check.get("ok") is False:
                preflight_issues += 1

    return {
        "total": total,
        "enabled": enabled,
        "manifest_ok": manifest_ok,
        "trusted": trusted,
        "runtime_covered": runtime_covered,
        "tests_present": tests_present,
        "preflight_ok": preflight_ok,
        "preflight_issues": preflight_issues,
        "enabled_pct": pct(enabled),
        "trusted_pct": pct(trusted),
        "preflight_ok_pct": pct(preflight_ok),
    }


def _set_skill_enabled_batch(
    *,
    loader,
    names: list[str] | None = None,
    enabled: bool,
) -> dict[str, Any]:
    requested_names = [str(item or "").strip() for item in list(names or []) if str(item or "").strip()]
    inventory = {row["name"]: row for row in loader.list_skills(filter_unavailable=False)}
    missing_names = sorted(name for name in requested_names if name not in inventory)
    rows: list[dict[str, Any]] = []
    for skill_name in requested_names:
        if skill_name not in inventory:
            continue
        ok = bool(loader.set_skill_enabled(skill_name, enabled))
        rows.append(
            {
                "name": skill_name,
                "enabled": bool(enabled),
                "ok": ok,
                "reload_required": False,
                "apply_state": _build_apply_state(
                    surface="skill",
                    target=skill_name,
                    reload_required=False,
                    execution_mode="in_process",
                ),
            }
        )
    return {
        "rows": rows,
        "requested_names": requested_names,
        "missing_names": missing_names,
        "enabled": bool(enabled),
        "changed_count": len([row for row in rows if bool(row.get("ok"))]),
        "reload_required": False,
        "apply_state": _build_apply_state(
            surface="skill",
            target="batch",
            reload_required=False,
            execution_mode="in_process",
        ),
    }


def _read_skill_detail(*, name: str) -> JSONResponse:
    from zen_claw.agent.skills import SkillsLoader
    from zen_claw.config.loader import get_data_dir, load_config

    cfg = load_config()
    data_dir = get_data_dir()
    loader = SkillsLoader(cfg.workspace_path)
    skill_name = str(name or "").strip()
    inventory_rows = {
        str(row.get("name") or "").strip(): row
        for row in list(loader.build_skills_inventory().get("skills") or [])
        if isinstance(row, dict)
    }
    row = inventory_rows.get(skill_name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {skill_name}")

    manifest, manifest_load_errors = loader.get_skill_manifest(skill_name)
    preflight = _run_skill_preflight(loader=loader, name=skill_name)
    package_state = _read_skill_package_state(loader=loader, skill_name=skill_name, data_dir=data_dir)
    package_policy = _build_skill_package_policy(
        data_dir=data_dir,
        skill_name=skill_name,
        package_state=package_state,
    )
    recent_actions = sorted(
        [
            item
            for item in _read_jsonl(data_dir / "dashboard" / "skills_actions.log.jsonl", limit=50)
            if isinstance(item, dict) and str(item.get("skill_name") or "").strip() == skill_name
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:5]
    recent_checks = sorted(
        [
            item
            for item in _read_jsonl(data_dir / "dashboard" / "skills_checks.log.jsonl", limit=50)
            if isinstance(item, dict) and str(item.get("skill_name") or "").strip() == skill_name
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:5]
    recent_exports = sorted(
        [
            item
            for item in _read_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", limit=50)
            if isinstance(item, dict) and str(item.get("skill_name") or "").strip() == skill_name
        ],
        key=lambda x: int(x.get("at_ms") or 0),
        reverse=True,
    )[:5]
    return JSONResponse(
        {
            "ok": True,
            "name": skill_name,
            "enabled": bool(loader.is_skill_enabled(skill_name)),
            "inventory": row,
            "manifest": manifest,
            "manifest_load_errors": manifest_load_errors,
            "preflight": preflight,
            "recent_actions": recent_actions,
            "recent_checks": recent_checks,
            "recent_exports": recent_exports,
            "package_state": package_state,
            "package_policy": package_policy,
            "governance": {
                "source_url": str((manifest or {}).get("source_url") or "") if isinstance(manifest, dict) else "",
                "source_version": str((manifest or {}).get("source_version") or "") if isinstance(manifest, dict) else "",
                "last_sync_checked_at": str((manifest or {}).get("last_sync_checked_at") or "") if isinstance(manifest, dict) else "",
                "intake_status": str((manifest or {}).get("intake_status") or "local") if isinstance(manifest, dict) else "local",
                "promote_status": str((manifest or {}).get("promote_status") or "candidate") if isinstance(manifest, dict) else "candidate",
                "promote_target_intent": str((manifest or {}).get("promote_target_intent") or "") if isinstance(manifest, dict) else "",
                "intake_summary": row.get("intake_summary") if isinstance(row, dict) else {},
            },
        }
    )


def _read_skill_activity_history(
    *,
    data_dir: Path,
    skill_name: str = "",
    event_kind: str = "",
    actor: str = "",
    ok_only: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_skill = str(skill_name or "").strip().lower()
    normalized_kind = str(event_kind or "").strip().lower()
    normalized_actor = str(actor or "").strip().lower()
    normalized_ok = str(ok_only or "").strip().lower()
    safe_limit = max(1, min(int(limit or 20), 50))
    safe_offset = max(0, int(offset or 0))

    rows: list[dict[str, Any]] = []
    for item in _read_jsonl(data_dir / "dashboard" / "skills_actions.log.jsonl", limit=100):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "at_ms": int(item.get("at_ms") or 0),
                "skill_name": str(item.get("skill_name") or ""),
                "event_kind": "action",
                "action": str(item.get("action") or ""),
                "ok": None,
                "actor": str(item.get("actor") or ""),
                "detail": str(item.get("detail") or item.get("action") or ""),
                "trace_id": str(item.get("trace_id") or ""),
            }
        )
    for item in _read_jsonl(data_dir / "dashboard" / "skills_checks.log.jsonl", limit=100):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "at_ms": int(item.get("at_ms") or 0),
                "skill_name": str(item.get("skill_name") or ""),
                "event_kind": "check",
                "action": "preflight",
                "ok": bool(item.get("ok")),
                "actor": str(item.get("actor") or ""),
                "detail": str(item.get("detail") or ("ok" if item.get("ok") else "issues")),
                "trace_id": str(item.get("trace_id") or ""),
            }
        )
    for item in _read_jsonl(data_dir / "dashboard" / "skills_exports.log.jsonl", limit=100):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "at_ms": int(item.get("at_ms") or 0),
                "skill_name": str(item.get("skill_name") or ""),
                "event_kind": "export",
                "action": "export",
                "ok": True,
                "actor": str(item.get("actor") or ""),
                "detail": str(item.get("out_path") or item.get("message") or "export"),
                "trace_id": str(item.get("trace_id") or ""),
            }
        )
    rows.sort(key=lambda row: int(row.get("at_ms") or 0), reverse=True)
    if normalized_skill:
        rows = [row for row in rows if str(row.get("skill_name") or "").strip().lower() == normalized_skill]
    if normalized_kind:
        rows = [row for row in rows if str(row.get("event_kind") or "").strip().lower() == normalized_kind]
    if normalized_actor:
        rows = [row for row in rows if str(row.get("actor") or "").strip().lower() == normalized_actor]
    if normalized_ok in {"true", "1", "yes", "ok"}:
        rows = [row for row in rows if row.get("ok") is True]
    elif normalized_ok in {"false", "0", "no", "fail"}:
        rows = [row for row in rows if row.get("ok") is False]
    page = rows[safe_offset : safe_offset + safe_limit]
    return {
        "skill_name": str(skill_name or ""),
        "event_kind": str(event_kind or ""),
        "actor": str(actor or ""),
        "ok_only": str(ok_only or ""),
        "limit": safe_limit,
        "offset": safe_offset,
        "returned": len(page),
        "total": len(rows),
        "rows": page,
    }


def _append_channel_action_audit(
    *,
    data_dir: Path,
    channel_name: str,
    action: str,
    enabled: bool,
    actor: str,
    detail: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "channels.state.change",
        "channel_name": str(channel_name),
        "action": str(action),
        "enabled": bool(enabled),
        "actor": str(actor or "api_key"),
        "detail": str(detail or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "channel_actions.log.jsonl", event)
    return event


def _append_channel_apply_audit(
    *,
    data_dir: Path,
    actor: str,
    note: str,
    target: str = "",
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    trace_id = uuid.uuid4().hex[:12]
    event = {
        "at_ms": now_ms,
        "trace_id": trace_id,
        "event": "channels.config.applied",
        "actor": str(actor or "api_key"),
        "note": str(note or "applied"),
        "target": str(target or ""),
    }
    _append_jsonl(data_dir / "dashboard" / "channel_apply.log.jsonl", event)
    return event


def _append_crawler_source_audit(
    *,
    data_dir: Path,
    source: Any,
    actor: str,
) -> dict[str, Any]:
    event = {
        "at_ms": int(time.time() * 1000),
        "trace_id": uuid.uuid4().hex[:12],
        "event": "crawler.source.upsert",
        "source_name": str(getattr(source, "name", "") or ""),
        "source_url": str(getattr(source, "url", "") or ""),
        "notebook_id": str(getattr(source, "notebook_id", "") or ""),
        "use_browser": bool(getattr(source, "use_browser", False)),
        "selector": str(getattr(source, "selector", "") or ""),
        "actor": str(actor or "api_key"),
    }
    _append_jsonl(data_dir / "dashboard" / "crawler_sources.log.jsonl", event)
    return event


def _append_crawler_run_audit(
    *,
    data_dir: Path,
    source: Any,
    payload: dict[str, Any],
    actor: str,
    status: str,
    error: str,
) -> dict[str, Any]:
    event = {
        "at_ms": int(time.time() * 1000),
        "trace_id": uuid.uuid4().hex[:12],
        "event": "crawler.run",
        "status": str(status or ""),
        "source_name": str(getattr(source, "name", "") or ""),
        "source_url": str(getattr(source, "url", "") or ""),
        "notebook_id": str(getattr(source, "notebook_id", "") or ""),
        "crawl_mode": str(payload.get("crawl_mode") or ("browser" if getattr(source, "use_browser", False) else "http")),
        "fetch_strategy": str(
            payload.get("fetch_strategy")
            or payload.get("crawl_mode")
            or ("browser" if getattr(source, "use_browser", False) else "http")
        ),
        "attempt_count": int(payload.get("attempt_count", 1) or 1),
        "retry_count": int(payload.get("retry_count", 0) or 0),
        "change_status": str(payload.get("change_status") or ""),
        "documents": int(payload.get("documents", 0) or 0),
        "chunks_added": int(payload.get("chunks_added", 0) or 0),
        "pages_fetched": int(payload.get("pages_fetched", 1) or 1),
        "paginated": bool(payload.get("paginated", False)),
        "error": str(error or ""),
        "actor": str(actor or "api_key"),
    }
    _append_jsonl(data_dir / "dashboard" / "crawler_runs.log.jsonl", event)
    return event


def _read_recent_crawler_runs(*, data_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    rows = [
        row
        for row in _read_jsonl(data_dir / "dashboard" / "crawler_runs.log.jsonl", limit=limit)
        if isinstance(row, dict)
    ]
    rows.extend(
        row
        for row in _read_jsonl(data_dir / "dashboard" / "crawler_cron.log.jsonl", limit=limit)
        if isinstance(row, dict)
    )
    rows.sort(key=lambda x: int(x.get("at_ms") or 0), reverse=True)
    return rows[:limit]


def _build_apply_state(
    *,
    surface: str,
    target: str,
    reload_required: bool,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    mode = str(execution_mode or ("config_reload" if reload_required else "in_process")).strip()
    status = "pending" if reload_required else "applied"
    return {
        "surface": str(surface or "").strip(),
        "target": str(target or "").strip(),
        "required": bool(reload_required),
        "status": status,
        "execution_mode": mode,
    }


def _update_agent_planning(*, agent_id: str, enabled: bool) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid == "default":
        cfg.agents.defaults.enable_planning = bool(enabled)
    else:
        profile = cfg.agents.profiles.get(aid)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")
        if not isinstance(profile, AgentProfileConfig):
            profile = AgentProfileConfig.model_validate(profile)
            cfg.agents.profiles[aid] = profile
        profile.enable_planning = bool(enabled)
    save_config(cfg, get_config_path())
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="planning_enable" if enabled else "planning_disable",
        planning_enabled=bool(enabled),
        actor="api_key",
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "planning_enabled": bool(enabled),
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _update_agent_model(*, agent_id: str, model: str) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    new_model = str(model or "").strip()
    if not new_model:
        raise HTTPException(status_code=400, detail="model is required")

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid == "default":
        before_model = str(cfg.agents.defaults.model or "").strip()
        cfg.agents.defaults.model = new_model
        planning_enabled = bool(cfg.agents.defaults.enable_planning)
    else:
        profile = cfg.agents.profiles.get(aid)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")
        if not isinstance(profile, AgentProfileConfig):
            profile = AgentProfileConfig.model_validate(profile)
            cfg.agents.profiles[aid] = profile
        before_model = str(profile.model or "").strip() or str(cfg.agents.defaults.model or "").strip()
        profile.model = new_model
        planning_enabled = bool(
            profile.enable_planning
            if profile.enable_planning is not None
            else cfg.agents.defaults.enable_planning
        )
    save_config(cfg, get_config_path())
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="model_update",
        planning_enabled=planning_enabled,
        actor="api_key",
        detail=f"{before_model or '-'} -> {new_model}",
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "model": new_model,
            "before_model": before_model,
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _update_agent_model_policy(
    *,
    agent_id: str,
    vision_model: str | None = None,
    thinking_model: str | None = None,
    fallback_model: str | None = None,
    cost_model: str | None = None,
    stability_model: str | None = None,
    intent_model_overrides: dict[str, str] | None = None,
    task_type_model_overrides: dict[str, str] | None = None,
    allowed_models: list[str] | None = None,
) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    defaults = cfg.agents.defaults

    def _normalize_model_name(value: str | None) -> str:
        return str(value or "").strip()

    def _normalize_overrides(value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        normalized: dict[str, str] = {}
        for key, model_name in dict(value or {}).items():
            intent_name = str(key or "").strip()
            selected_model = str(model_name or "").strip()
            if intent_name and selected_model:
                normalized[intent_name] = selected_model
        return normalized

    def _normalize_allowed_models(value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item for item in (str(entry or "").strip() for entry in list(value or [])) if item]

    next_overrides = _normalize_overrides(intent_model_overrides)
    next_task_type_overrides = _normalize_overrides(task_type_model_overrides)
    next_allowed_models = _normalize_allowed_models(allowed_models)
    next_vision_model = _normalize_model_name(vision_model) if vision_model is not None else None
    next_thinking_model = (
        _normalize_model_name(thinking_model) if thinking_model is not None else None
    )
    next_fallback_model = (
        _normalize_model_name(fallback_model) if fallback_model is not None else None
    )
    next_cost_model = _normalize_model_name(cost_model) if cost_model is not None else None
    next_stability_model = (
        _normalize_model_name(stability_model) if stability_model is not None else None
    )

    if aid == "default":
        target = cfg.agents.defaults
        planning_enabled = bool(cfg.agents.defaults.enable_planning)
    else:
        profile = cfg.agents.profiles.get(aid)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")
        if not isinstance(profile, AgentProfileConfig):
            profile = AgentProfileConfig.model_validate(profile)
        target = profile
        planning_enabled = bool(
            profile.enable_planning
            if profile.enable_planning is not None
            else cfg.agents.defaults.enable_planning
        )

    before_policy = {
        "vision_model": (
            str(target.vision_model or "").strip()
            or (str(defaults.vision_model or "").strip() if aid != "default" else "")
        ),
        "thinking_model": (
            str(target.thinking_model or "").strip()
            or (str(defaults.thinking_model or "").strip() if aid != "default" else "")
        ),
        "fallback_model": (
            str(target.fallback_model or "").strip()
            or (str(defaults.fallback_model or "").strip() if aid != "default" else "")
        ),
        "cost_model": (
            str(target.cost_model or "").strip()
            or (str(defaults.cost_model or "").strip() if aid != "default" else "")
        ),
        "stability_model": (
            str(target.stability_model or "").strip()
            or (str(defaults.stability_model or "").strip() if aid != "default" else "")
        ),
        "intent_model_overrides": (
            dict(target.intent_model_overrides or {})
            if aid == "default" or target.intent_model_overrides
            else dict(defaults.intent_model_overrides or {})
        ),
        "task_type_model_overrides": (
            dict(target.task_type_model_overrides or {})
            if aid == "default" or target.task_type_model_overrides
            else dict(defaults.task_type_model_overrides or {})
        ),
        "allowed_models": (
            list(target.allowed_models or [])
            if aid == "default" or target.allowed_models
            else list(defaults.allowed_models or [])
        ),
    }

    if (
        next_vision_model is None
        and next_thinking_model is None
        and next_fallback_model is None
        and next_cost_model is None
        and next_stability_model is None
        and next_overrides is None
        and next_task_type_overrides is None
        and next_allowed_models is None
    ):
        raise HTTPException(status_code=400, detail="at least one model policy field is required")

    if next_vision_model is not None:
        target.vision_model = next_vision_model
    if next_thinking_model is not None:
        target.thinking_model = next_thinking_model
    if next_fallback_model is not None:
        target.fallback_model = next_fallback_model
    if next_cost_model is not None:
        target.cost_model = next_cost_model
    if next_stability_model is not None:
        target.stability_model = next_stability_model
    if next_overrides is not None:
        target.intent_model_overrides = dict(next_overrides)
    if next_task_type_overrides is not None:
        target.task_type_model_overrides = dict(next_task_type_overrides)
    if next_allowed_models is not None:
        target.allowed_models = list(next_allowed_models)

    if aid != "default":
        target = AgentProfileConfig.model_validate(target.model_dump(mode="python"))
        cfg.agents.profiles[aid] = target

    save_config(cfg, get_config_path())

    after_policy = {
        "vision_model": (
            str(target.vision_model or "").strip()
            or (str(defaults.vision_model or "").strip() if aid != "default" else "")
        ),
        "thinking_model": (
            str(target.thinking_model or "").strip()
            or (str(defaults.thinking_model or "").strip() if aid != "default" else "")
        ),
        "fallback_model": (
            str(target.fallback_model or "").strip()
            or (str(defaults.fallback_model or "").strip() if aid != "default" else "")
        ),
        "cost_model": (
            str(target.cost_model or "").strip()
            or (str(defaults.cost_model or "").strip() if aid != "default" else "")
        ),
        "stability_model": (
            str(target.stability_model or "").strip()
            or (str(defaults.stability_model or "").strip() if aid != "default" else "")
        ),
        "intent_model_overrides": (
            dict(target.intent_model_overrides or {})
            if aid == "default" or target.intent_model_overrides
            else dict(defaults.intent_model_overrides or {})
        ),
        "task_type_model_overrides": (
            dict(target.task_type_model_overrides or {})
            if aid == "default" or target.task_type_model_overrides
            else dict(defaults.task_type_model_overrides or {})
        ),
        "allowed_models": (
            list(target.allowed_models or [])
            if aid == "default" or target.allowed_models
            else list(defaults.allowed_models or [])
        ),
    }
    detail_parts: list[str] = []
    for key in (
        "vision_model",
        "thinking_model",
        "fallback_model",
        "cost_model",
        "stability_model",
    ):
        if before_policy[key] != after_policy[key]:
            detail_parts.append(f"{key}: {before_policy[key] or '-'} -> {after_policy[key] or '-'}")
    if before_policy["intent_model_overrides"] != after_policy["intent_model_overrides"]:
        detail_parts.append(
            f"intent overrides: {len(before_policy['intent_model_overrides'])} -> {len(after_policy['intent_model_overrides'])}"
        )
    if before_policy["task_type_model_overrides"] != after_policy["task_type_model_overrides"]:
        detail_parts.append(
            f"task-type overrides: {len(before_policy['task_type_model_overrides'])} -> {len(after_policy['task_type_model_overrides'])}"
        )
    if before_policy["allowed_models"] != after_policy["allowed_models"]:
        detail_parts.append(
            f"allowed models: {len(before_policy['allowed_models'])} -> {len(after_policy['allowed_models'])}"
        )
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="model_policy_update",
        planning_enabled=planning_enabled,
        actor="api_key",
        detail=" | ".join(detail_parts) or "model policy updated",
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "before_policy": before_policy,
            "policy": after_policy,
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _update_agent_routing_keywords(*, agent_id: str, routing_keywords: list[str]) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid == "default":
        raise HTTPException(status_code=400, detail="default agent does not support routing keywords")

    profile = cfg.agents.profiles.get(aid)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")
    if not isinstance(profile, AgentProfileConfig):
        profile = AgentProfileConfig.model_validate(profile)
        cfg.agents.profiles[aid] = profile

    before_keywords = list(profile.routing_keywords or [])
    profile.routing_keywords = list(routing_keywords or [])
    profile = AgentProfileConfig.model_validate(profile.model_dump(mode="python"))
    cfg.agents.profiles[aid] = profile
    save_config(cfg, get_config_path())
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="routing_keywords_update",
        planning_enabled=bool(
            profile.enable_planning
            if profile.enable_planning is not None
            else cfg.agents.defaults.enable_planning
        ),
        actor="api_key",
        detail=(
            f"{', '.join(before_keywords) if before_keywords else '-'} -> "
            f"{', '.join(profile.routing_keywords) if profile.routing_keywords else '-'}"
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "routing_keywords": list(profile.routing_keywords or []),
            "before_routing_keywords": before_keywords,
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _save_agent_profile(*, agent_id: str, profile_payload: dict[str, Any]) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid == "default":
        raise HTTPException(status_code=400, detail="default agent cannot be replaced as a named profile")
    if not isinstance(profile_payload, dict):
        raise HTTPException(status_code=400, detail="profile payload must be an object")

    before_profile = cfg.agents.profiles.get(aid)
    before_dump = (
        AgentProfileConfig.model_validate(before_profile).model_dump(mode="python")
        if before_profile is not None
        else None
    )
    try:
        profile = AgentProfileConfig.model_validate(profile_payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid profile payload: {exc}") from exc
    cfg.agents.profiles[aid] = profile
    save_config(cfg, get_config_path())
    after_dump = profile.model_dump(mode="python")
    changed_fields = sorted(
        key
        for key in set((before_dump or {}).keys()) | set(after_dump.keys())
        if (before_dump or {}).get(key) != after_dump.get(key)
    )
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="profile_create" if before_profile is None else "profile_update",
        planning_enabled=bool(
            profile.enable_planning
            if profile.enable_planning is not None
            else cfg.agents.defaults.enable_planning
        ),
        actor="api_key",
        detail=(
            "created profile"
            if before_profile is None
            else f"updated fields: {', '.join(changed_fields) if changed_fields else 'none'}"
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "created": before_profile is None,
            "profile": after_dump,
            "before_profile": before_dump,
            "changed_fields": changed_fields,
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _delete_agent_profile(*, agent_id: str) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config
    from zen_claw.config.schema import AgentProfileConfig

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid == "default":
        raise HTTPException(status_code=400, detail="default agent cannot be deleted")
    profile = cfg.agents.profiles.get(aid)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")
    before_dump = AgentProfileConfig.model_validate(profile).model_dump(mode="python")
    del cfg.agents.profiles[aid]
    save_config(cfg, get_config_path())
    channel_refs = [
        {"channel": str(spec.key or ""), "display_name": str(row.get("display_name") or spec.display_name or "")}
        for spec in iter_channel_specs()
        for row in [build_channel_rbac_row(cfg, spec)]
        if str(getattr(getattr(cfg.channels, str(spec.config_attr or ""), None), "agent_profile", "") or "")
        .strip()
        .lower()
        == aid
    ]
    _append_agent_action_audit(
        data_dir=get_data_dir(),
        agent_id=aid,
        action="profile_delete",
        planning_enabled=False,
        actor="api_key",
        detail=(
            f"deleted profile; channel references={len(channel_refs)}"
            if channel_refs
            else "deleted profile"
        ),
    )
    return JSONResponse(
        {
            "ok": True,
            "agent_id": aid,
            "deleted": True,
            "before_profile": before_dump,
            "channel_references": channel_refs,
            "reload_required": True,
            "apply_state": _build_apply_state(surface="agent", target=aid, reload_required=True),
        }
    )


def _resolve_agent_route_store(config: Config):
    from zen_claw.channels.routing import AgentRouteStore
    from zen_claw.config.loader import get_data_dir

    candidates: list[Path] = []
    try:
        candidates.append(get_data_dir() / "channels" / "agent_routes.db")
    except Exception:
        pass
    candidates.append(config.workspace_path / ".zen-claw" / "channels" / "agent_routes.db")
    for db_path in candidates:
        if not db_path.exists():
            continue
        try:
            return AgentRouteStore(db_path)
        except Exception:
            continue
    return None


def _ensure_agent_route_store(config: Config):
    from zen_claw.channels.routing import AgentRouteStore
    from zen_claw.config.loader import get_data_dir

    try:
        return AgentRouteStore(get_data_dir() / "channels" / "agent_routes.db")
    except Exception:
        return AgentRouteStore(config.workspace_path / ".zen-claw" / "channels" / "agent_routes.db")


def _preview_agent_route(
    *,
    channel: str,
    chat_id: str,
    user_id: str,
    content: str,
    explicit_agent_id: str = "",
) -> JSONResponse:
    from zen_claw.agent.router import AgentRouter
    from zen_claw.config.loader import load_config

    channel_name = str(channel or "").strip()
    chat_key = str(chat_id or "").strip()
    user_key = str(user_id or "").strip()
    if not channel_name or not chat_key or not user_key:
        raise HTTPException(status_code=400, detail="channel/chat_id/user_id are required")

    cfg = load_config()
    route_store = _resolve_agent_route_store(cfg)

    def _resolve_bound_agent(route_channel: str, route_chat_id: str, route_user_id: str) -> str | None:
        if route_store is None:
            return None
        route = route_store.resolve_route(
            channel=route_channel,
            chat_id=route_chat_id,
            user_id=route_user_id,
        )
        return route.agent_id if route else None

    router = AgentRouter(cfg, resolve_bound_agent=_resolve_bound_agent)
    decision = router.resolve(
        channel=channel_name,
        chat_id=chat_key,
        user_id=user_key,
        content=content,
        explicit_agent_id=explicit_agent_id,
    )

    bound_agent_id = ""
    route_audit: list[dict[str, int | str]] = []
    if route_store is not None:
        route = route_store.resolve_route(channel=channel_name, chat_id=chat_key, user_id=user_key)
        bound_agent_id = route.agent_id if route else ""
        route_audit = route_store.list_audit(channel=channel_name, chat_id=chat_key, user_id=user_key)

    channels_cfg = getattr(cfg, "channels", None)
    channel_cfg = getattr(channels_cfg, channel_name.lower(), None) if channels_cfg is not None else None
    channel_profile = str(getattr(channel_cfg, "agent_profile", "") or "").strip()
    return JSONResponse(
        {
            "ok": True,
            "channel": channel_name,
            "chat_id": chat_key,
            "user_id": user_key,
            "content": str(content or ""),
            "explicit_agent_id": str(explicit_agent_id or "").strip(),
            "decision": {
                "agent_id": decision.agent_id,
                "reason": decision.reason,
                "matched_keyword": decision.matched_keyword,
                "registered": bool(decision.registered),
            },
            "bound_agent_id": bound_agent_id,
            "channel_profile": channel_profile,
            "route_audit": route_audit[-10:],
            "route_audit_total": len(route_audit),
        }
    )


def _bind_agent_route(
    *,
    channel: str,
    chat_id: str,
    user_id: str,
    agent_id: str,
) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id
    from zen_claw.config.loader import load_config

    channel_name = str(channel or "").strip()
    chat_key = str(chat_id or "").strip()
    user_key = str(user_id or "").strip()
    bound_agent = normalize_agent_id(agent_id)
    if not channel_name or not chat_key or not user_key:
        raise HTTPException(status_code=400, detail="channel/chat_id/user_id are required")
    if not bound_agent:
        raise HTTPException(status_code=400, detail="agent_id is required")

    cfg = load_config()
    if bound_agent != "default" and bound_agent not in cfg.agents.profiles:
        raise HTTPException(status_code=404, detail=f"agent profile not found: {bound_agent}")
    route_store = _ensure_agent_route_store(cfg)
    route = route_store.set_route(
        channel=channel_name,
        chat_id=chat_key,
        user_id=user_key,
        agent_id=bound_agent,
        reason="manual_bind",
    )
    route_audit = route_store.list_audit(channel=channel_name, chat_id=chat_key, user_id=user_key)
    return JSONResponse(
        {
            "ok": True,
            "channel": channel_name,
            "chat_id": chat_key,
            "user_id": user_key,
            "bound_agent_id": route.agent_id,
            "updated_at_ms": route.updated_at_ms,
            "route_audit": route_audit[-10:],
            "route_audit_total": len(route_audit),
            "reload_required": False,
        }
    )


def _clear_agent_route(
    *,
    channel: str,
    chat_id: str,
    user_id: str,
) -> JSONResponse:
    from zen_claw.config.loader import load_config

    channel_name = str(channel or "").strip()
    chat_key = str(chat_id or "").strip()
    user_key = str(user_id or "").strip()
    if not channel_name or not chat_key or not user_key:
        raise HTTPException(status_code=400, detail="channel/chat_id/user_id are required")

    cfg = load_config()
    route_store = _ensure_agent_route_store(cfg)
    event = route_store.clear_route(channel=channel_name, chat_id=chat_key, user_id=user_key)
    route_audit = route_store.list_audit(channel=channel_name, chat_id=chat_key, user_id=user_key)
    return JSONResponse(
        {
            "ok": True,
            "channel": channel_name,
            "chat_id": chat_key,
            "user_id": user_key,
            "cleared": True,
            "previous_agent_id": str(event.get("previous_agent_id") or ""),
            "bound_agent_id": "",
            "route_audit": route_audit[-10:],
            "route_audit_total": len(route_audit),
            "reload_required": False,
        }
    )


def _read_agent_profile_detail(*, agent_id: str) -> JSONResponse:
    from zen_claw.agent.pool import normalize_agent_id, resolve_agent_profile
    from zen_claw.config.loader import load_config

    cfg = load_config()
    aid = normalize_agent_id(agent_id)
    if aid != "default" and aid not in cfg.agents.profiles:
        raise HTTPException(status_code=404, detail=f"agent profile not found: {aid}")

    resolved = resolve_agent_profile(cfg, aid)
    raw_profile = cfg.agents.profiles.get(aid)
    channel_refs: list[dict[str, str]] = []
    for spec in iter_channel_specs():
        channel_cfg = getattr(cfg.channels, spec.config_attr, None)
        if channel_cfg is None:
            continue
        profile_id = str(getattr(channel_cfg, "agent_profile", "") or "").strip()
        if profile_id == aid:
            channel_refs.append(
                {
                    "channel": spec.key,
                    "display_name": spec.display_name,
                }
            )

    payload = {
        "ok": True,
        "agent_id": resolved.agent_id,
        "display_name": resolved.display_name,
        "description": resolved.description,
        "registered": bool(resolved.is_registered),
        "channel_references": channel_refs,
        "effective": {
            "workspace": str(resolved.workspace),
            "system_prompt": resolved.system_prompt,
            "system_prompt_file": resolved.system_prompt_file,
            "allowed_tools": list(resolved.allowed_tools or []),
            "denied_tools": list(resolved.denied_tools or []),
            "skill_names": list(resolved.skill_names or []),
            "model": resolved.model,
            "vision_model": resolved.vision_model,
            "thinking_model": resolved.thinking_model,
            "fallback_model": resolved.fallback_model,
            "cost_model": resolved.cost_model,
            "stability_model": resolved.stability_model,
            "intent_model_overrides": dict(resolved.intent_model_overrides),
            "task_type_model_overrides": dict(resolved.task_type_model_overrides),
            "memory_recall_mode": resolved.memory_recall_mode,
            "planning_enabled": bool(resolved.enable_planning),
            "max_reflections": int(resolved.max_reflections),
            "auto_parameter_rewrite": bool(resolved.auto_parameter_rewrite),
            "skill_permissions_mode": resolved.skill_permissions_mode,
            "max_tokens": int(resolved.max_tokens),
            "compression_trigger_ratio": float(resolved.compression_trigger_ratio),
            "compression_hysteresis_ratio": float(resolved.compression_hysteresis_ratio),
            "compression_cooldown_turns": int(resolved.compression_cooldown_turns),
            "max_tool_iterations": int(resolved.max_tool_iterations),
            "allowed_models": list(resolved.allowed_models or []),
        },
        "profile_overrides": (
            raw_profile.model_dump(mode="python") if raw_profile is not None else {}
        ),
    }
    return JSONResponse(payload)


def _update_channel_enabled(*, name: str, enabled: bool) -> JSONResponse:
    from zen_claw.config.loader import get_config_path, get_data_dir, load_config, save_config

    cfg = load_config()
    rows = {
        row["name"]: row for row in (build_channel_rbac_row(cfg, spec) for spec in iter_channel_specs())
    }
    if name not in rows:
        raise HTTPException(status_code=404, detail=f"channel not found: {name}")

    ch_cfg = getattr(cfg.channels, str(name))
    setattr(ch_cfg, "enabled", bool(enabled))
    save_config(cfg, get_config_path())
    _append_channel_action_audit(
        data_dir=get_data_dir(),
        channel_name=name,
        action="enable" if enabled else "disable",
        enabled=enabled,
        actor="api_key",
    )
    return JSONResponse(
        {
            "ok": True,
            "name": str(name),
            "enabled": bool(enabled),
            "reload_required": True,
            "apply_state": _build_apply_state(surface="channel", target=str(name), reload_required=True),
        }
    )


def _render_html(snapshot: dict[str, Any], refresh_sec: int) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>zen-claw Dashboard</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #102136;
      --muted: #526173;
      --accent: #0d6efd;
      --border: #d9e0ea;
    }}
    body {{ margin: 0; font-family: "Segoe UI", "PingFang SC", sans-serif; background: var(--bg); color: var(--ink); }}
    .wrap {{ max-width: 1100px; margin: 24px auto; padding: 0 16px; }}
    .hero {{ margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }}
    .grid-wide {{ display: grid; grid-template-columns: 1fr; gap: 12px; margin-top: 12px; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid var(--border); text-align: left; padding: 4px 6px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    h2 {{ margin: 0 0 6px; font-size: 16px; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    pre {{ margin: 0; max-height: 430px; overflow: auto; font-size: 12px; background: #f1f4f8; padding: 10px; border-radius: 8px; }}
    .ok {{ color: #087f23; }}
    .warn {{ color: #b26a00; }}
    .bad {{ color: #b42318; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>zen-claw Dashboard</h1>
      <div class="muted">Operational view. Auto refresh every {refresh_sec}s.</div>
      <div class="muted" style="margin-top: 6px;">
        Management API key:
        <input id="skillsApiKey" type="password" placeholder="X-API-Key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px;" />
        <button onclick="persistSkillsApiKey()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Save</button>
        <span id="skillsActionStatus" class="muted"></span>
      </div>
    </div>
    <div class="grid">
      <div class="card"><h2>Operations Summary</h2><div id="ops"></div></div>
      <div class="card"><h2>Agent</h2><div id="agent"></div></div>
      <div class="card"><h2>Agents</h2><div id="agents"></div></div>
      <div class="card"><h2>Model Usage</h2><div id="modelUsage"></div></div>
      <div class="card"><h2>Skills</h2><div id="skills"></div></div>
      <div class="card"><h2>Crawler</h2><div id="crawler"></div></div>
      <div class="card"><h2>Intent Router</h2><div id="intentRouter"></div></div>
      <div class="card"><h2>Workflow Webhooks</h2><div id="workflowWebhooks"></div></div>
      <div class="card"><h2>Channels</h2><div id="channels"></div></div>
      <div class="card"><h2>Cron</h2><div id="cron"></div></div>
      <div class="card"><h2>Knowledge</h2><div id="knowledge"></div></div>
      <div class="card"><h2>Security</h2><div id="security"></div></div>
      <div class="card"><h2>Sidecars</h2><div id="sidecars"></div></div>
      <div class="card"><h2>Node Queue</h2><div id="node"></div></div>
    </div>
    <div class="grid-wide">
      <div class="card"><h2>Recent Observability</h2><div id="recentObservability"></div></div>
      <div class="card"><h2>Node Details</h2><div id="nodeDetails"></div></div>
      <div class="card"><h2>Approval Timeline</h2><div id="approvalTimeline"></div></div>
      <div class="card"><h2>Compression Timeline</h2><div id="compressionTimeline"></div></div>
    </div>
    <div class="card" style="margin-top: 12px;">
      <h2>Raw Snapshot</h2>
      <pre id="raw">{escape(payload)}</pre>
    </div>
  </div>
  <script>
    const refreshMs = {max(refresh_sec, 1) * 1000};
    function p(id, html) {{ document.getElementById(id).innerHTML = html; }}
    function esc(value) {{
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}
    function getSkillsApiKey() {{
      return (document.getElementById("skillsApiKey")?.value || localStorage.getItem("zenClawSkillsApiKey") || "").trim();
    }}
    function persistSkillsApiKey() {{
      const value = (document.getElementById("skillsApiKey")?.value || "").trim();
      localStorage.setItem("zenClawSkillsApiKey", value);
      const status = document.getElementById("skillsActionStatus");
      if (status) status.textContent = value ? "saved" : "cleared";
    }}
    async function toggleSkill(name, enable) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill changes";
        return;
      }}
      if (status) status.textContent = `${{enable ? 'enabling' : 'disabling'}} ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/${{enable ? 'enable' : 'disable'}}`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{name}} -> ${{enable ? 'enabled' : 'disabled'}}`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill action failed: ${{err.message || err}}`;
      }}
    }}
    async function preflightSkill(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill checks";
        return;
      }}
      if (status) status.textContent = `checking ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/preflight`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{name}} preflight -> ${{body.ok ? 'ok' : 'issues found'}}`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill preflight failed: ${{err.message || err}}`;
      }}
    }}
    async function exportSkill(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill export";
        return;
      }}
      if (status) status.textContent = `exporting ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/export`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{name}} exported -> ${{body.out_path || '-'}}`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill export failed: ${{err.message || err}}`;
      }}
    }}
    async function restoreSkillExport(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill restore";
        return;
      }}
      if (status) status.textContent = `restoring ${{name}} from latest export...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/restore-export`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{name}} restored from export`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill restore failed: ${{err.message || err}}`;
      }}
    }}
    async function restoreSkill(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for skill restore";
        return;
      }}
      if (status) status.textContent = `restoring ${{name}} via package policy...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/restore`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const plan = body.restore_plan || {{}};
        const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
        if (detail) detail.innerHTML = `restore plan for <b>${{esc(name)}}</b>: source=<code>${{esc(plan.latest_export_path || body.source || '-')}}</code><br/>export dir/version: <code>${{esc(plan.export_physical_dir || '-')}}</code> / <b>${{esc(plan.export_version || '-')}}</b><br/>selected dir/version: <code>${{esc(plan.selected_physical_dir || '-')}}</code> / <b>${{esc(plan.selected_version || '-')}}</b><br/>preferred dir/version: <code>${{esc(plan.preferred_physical_dir || '-')}}</code> / <b>${{esc(plan.preferred_version || '-')}}</b><br/>warnings: ${{warnings.length ? warnings.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}`;
        if (status) status.textContent = `${{name}} restored via package policy`;
        await inspectSkill(name);
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill restore failed: ${{err.message || err}}`;
      }}
    }}
    async function previewSkillRestore(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for skill restore preview";
        return;
      }}
      if (status) status.textContent = `previewing restore for ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/restore-plan`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const plan = body.restore_plan || {{}};
        const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
        const blocked = Array.isArray(plan.blocked_reasons) ? plan.blocked_reasons : [];
        if (detail) detail.innerHTML = `restore plan for <b>${{esc(name)}}</b>: ready=<b>${{plan.ready ? 'yes' : 'no'}}</b><br/>source=<code>${{esc(plan.latest_export_path || '-')}}</code><br/>export dir/version: <code>${{esc(plan.export_physical_dir || '-')}}</code> / <b>${{esc(plan.export_version || '-')}}</b><br/>selected dir/version: <code>${{esc(plan.selected_physical_dir || '-')}}</code> / <b>${{esc(plan.selected_version || '-')}}</b><br/>preferred dir/version: <code>${{esc(plan.preferred_physical_dir || '-')}}</code> / <b>${{esc(plan.preferred_version || '-')}}</b><br/>blocked: ${{blocked.length ? blocked.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}<br/>warnings: ${{warnings.length ? warnings.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}`;
        if (status) status.textContent = `${{name}} restore preview ready`;
      }} catch (err) {{
        if (status) status.textContent = `skill restore preview failed: ${{err.message || err}}`;
      }}
    }}
    async function previewSkillGc() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      const retentionHours = Number((document.getElementById("skillGcRetentionHours")?.value || "24").trim() || 24);
      if (!key) {{
        if (status) status.textContent = "API key required for skill cleanup preview";
        return;
      }}
      if (status) status.textContent = "previewing stale skill versions...";
      try {{
        const resp = await fetch("/api/v1/skills/gc-preview", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ retention_hours: retentionHours }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.candidates) ? body.candidates.slice(0, 8) : [];
        const rowsHtml = rows.map(row => `<code>${{esc(row.physical_dir || '-')}}</code> <span class="muted">${{esc(String(row.age_hours ?? '-'))}}h</span>`).join("<br/>");
        if (detail) detail.innerHTML = `stale package cleanup preview: <b>${{body.candidate_count || 0}}</b> candidate(s)<br/>retention hours: <b>${{body.retention_hours || 0}}</b><br/>candidates: ${{rowsHtml || '<span class="muted">none</span>'}}`;
        if (status) status.textContent = "skill cleanup preview ready";
      }} catch (err) {{
        if (status) status.textContent = `skill cleanup preview failed: ${{err.message || err}}`;
      }}
    }}
    async function runSkillGc() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const retentionHours = Number((document.getElementById("skillGcRetentionHours")?.value || "24").trim() || 24);
      if (!key) {{
        if (status) status.textContent = "API key required for skill cleanup";
        return;
      }}
      if (status) status.textContent = "cleaning stale skill versions...";
      try {{
        const resp = await fetch("/api/v1/skills/gc-run", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ retention_hours: retentionHours }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `skill cleanup deleted ${{body.deleted_count || 0}} stale package(s)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill cleanup failed: ${{err.message || err}}`;
      }}
    }}
    async function installSkill() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const source = (document.getElementById("skillInstallSource")?.value || "").trim();
      const name = (document.getElementById("skillInstallName")?.value || "").trim();
      const overwrite = !!document.getElementById("skillInstallOverwrite")?.checked;
      const strictManifest = !!document.getElementById("skillInstallStrictManifest")?.checked;
      const sanitize = !!document.getElementById("skillInstallSanitize")?.checked;
      if (!key) {{
        if (status) status.textContent = "API key required for skill install";
        return;
      }}
      if (!source) {{
        if (status) status.textContent = "source path is required";
        return;
      }}
      if (status) status.textContent = `installing skill from ${{source}}...`;
      try {{
        const resp = await fetch("/api/v1/skills/install", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            source,
            name: name || null,
            overwrite,
            strict_manifest: strictManifest,
            sanitize
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{body.name || '-'}} installed`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill install failed: ${{err.message || err}}`;
      }}
    }}
    async function setSkillBatchState(enable) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for batch skill state updates";
        return;
      }}
      const rawNames = (document.getElementById("skillBatchStateNames")?.value || "").trim();
      const names = rawNames ? rawNames.split(",").map(item => item.trim()).filter(Boolean) : [];
      if (!names.length) {{
        if (status) status.textContent = "at least one skill name is required";
        return;
      }}
      if (status) status.textContent = `${{enable ? 'enabling' : 'disabling'}} selected skills...`;
      try {{
        const resp = await fetch(enable ? "/api/v1/skills/enable-batch" : "/api/v1/skills/disable-batch", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ names }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows.slice(0, 8) : [];
        const rowsHtml = rows.map(row => `<tr><td><code>${{esc(row.name || '-')}}</code></td><td>${{row.ok ? 'ok' : 'missing'}}</td><td>${{row.enabled ? 'enabled' : 'disabled'}}</td></tr>`).join('');
        const missing = Array.isArray(body.missing_names) ? body.missing_names.join(", ") : "";
        if (detail) detail.innerHTML = `changed: <b>${{body.changed_count || 0}}</b><br/>missing: <b>${{esc(missing || '-')}}</b><br/><table><thead><tr><th>Skill</th><th>Status</th><th>State</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="3" class="muted">No changes</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = `batch skill state updated: ${{body.changed_count || 0}} changed`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `batch skill state update failed: ${{err.message || err}}`;
      }}
    }}
    async function bulkPreflightSkills() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for batch skill checks";
        return;
      }}
      const rawNames = (document.getElementById("skillBatchPreflightNames")?.value || "").trim();
      const includeDisabled = !!document.getElementById("skillBatchPreflightIncludeDisabled")?.checked;
      const names = rawNames ? rawNames.split(",").map(item => item.trim()).filter(Boolean) : [];
      if (status) status.textContent = "running batch skill preflight...";
      try {{
        const resp = await fetch("/api/v1/skills/preflight-batch", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            names,
            include_disabled: includeDisabled
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows.slice(0, 8) : [];
        const rowsHtml = rows.map(row => `<tr><td><code>${{esc(row.name || '-')}}</code></td><td>${{row.ok ? 'ok' : 'issues'}}</td><td>${{esc(row.detail || '-')}}</td><td>${{row.tests_present ? 'yes' : 'no'}}</td></tr>`).join('');
        const missing = Array.isArray(body.missing_names) ? body.missing_names.join(", ") : "";
        if (detail) detail.innerHTML = `checked / ok / issues: <b>${{body.checked_count || 0}}</b> / <b>${{body.ok_count || 0}}</b> / <b>${{body.issue_count || 0}}</b><br/>missing: <b>${{esc(missing || '-')}}</b><br/><table><thead><tr><th>Skill</th><th>Status</th><th>Detail</th><th>Tests</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="4" class="muted">No skills selected</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "batch skill preflight ready";
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `batch skill preflight failed: ${{err.message || err}}`;
      }}
    }}
    async function loadSkillActivityHistory() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillHistoryStatus");
      const result = document.getElementById("skillHistoryPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for skill history";
        return;
      }}
      const skillName = (document.getElementById("skillHistoryNameFilter")?.value || "").trim();
      const eventKind = (document.getElementById("skillHistoryKindFilter")?.value || "").trim();
      const actor = (document.getElementById("skillHistoryActorFilter")?.value || "").trim();
      const okOnly = (document.getElementById("skillHistoryOkFilter")?.value || "").trim();
      const limit = (document.getElementById("skillHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("skillHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "loading skill history...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (skillName) qs.set("skill_name", skillName);
        if (eventKind) qs.set("event_kind", eventKind);
        if (actor) qs.set("actor", actor);
        if (okOnly) qs.set("ok_only", okOnly);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        const resp = await fetch(`/api/v1/skills/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const rowsHtml = rows.map(row => `<tr><td>${{fmtAgo(row.at_ms, Date.now())}}</td><td><code>${{esc(row.skill_name || '-')}}</code></td><td>${{esc(row.event_kind || '-')}}</td><td>${{esc(row.detail || row.action || '-')}}</td><td>${{row.ok === null || row.ok === undefined ? '-' : (row.ok ? 'ok' : 'issues')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
        if (result) result.innerHTML = `returned / total: <b>${{body.returned || 0}}</b> / <b>${{body.total || 0}}</b><br/><table><thead><tr><th>When</th><th>Skill</th><th>Kind</th><th>Detail</th><th>OK</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="6" class="muted">No matching skill activity</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "skill history ready";
      }} catch (err) {{
        if (status) status.textContent = `skill history failed: ${{err.message || err}}`;
      }}
    }}
    async function exportSkillActivityHistoryCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillHistoryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill history export";
        return;
      }}
      const skillName = (document.getElementById("skillHistoryNameFilter")?.value || "").trim();
      const eventKind = (document.getElementById("skillHistoryKindFilter")?.value || "").trim();
      const actor = (document.getElementById("skillHistoryActorFilter")?.value || "").trim();
      const okOnly = (document.getElementById("skillHistoryOkFilter")?.value || "").trim();
      const limit = (document.getElementById("skillHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("skillHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "exporting skill history CSV...";
      try {{
        const qs = new URLSearchParams();
        if (skillName) qs.set("skill_name", skillName);
        if (eventKind) qs.set("event_kind", eventKind);
        if (actor) qs.set("actor", actor);
        if (okOnly) qs.set("ok_only", okOnly);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/skills/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "skill-activity-history.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "skill history CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `skill history export failed: ${{err.message || err}}`;
      }}
    }}
    async function inspectSkill(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      const detail = document.getElementById("skillsDetailPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for skill inspection";
        return;
      }}
      if (status) status.textContent = `loading skill details for ${{name}}...`;
      if (detail) detail.innerHTML = "";
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const preflight = body.preflight || {{}};
        const manifest = body.manifest || null;
        const packageState = body.package_state || {{}};
        const packagePolicy = body.package_policy || {{}};
        const manifestErrors = [...(body.manifest_load_errors || []), ...(preflight.manifest_errors || [])];
        const integrityErrors = preflight.integrity_errors || [];
        const actions = Array.isArray(body.recent_actions) ? body.recent_actions.slice(0, 3) : [];
        const checks = Array.isArray(body.recent_checks) ? body.recent_checks.slice(0, 3) : [];
        const exports = Array.isArray(body.recent_exports) ? body.recent_exports.slice(0, 3) : [];
        const packageRows = Array.isArray(packageState.package_rows) ? packageState.package_rows.slice(0, 6) : [];
        const journalRows = Array.isArray(packageState.journal_entries) ? packageState.journal_entries.slice(0, 3) : [];
        const packageWarnings = Array.isArray(packagePolicy.warnings) ? packagePolicy.warnings : [];
        const actionsHtml = actions.map(row => `${{fmtAgo(row.at_ms, Date.now())}}: ${{esc(row.detail || row.action || '-')}}`).join("<br/>");
        const checksHtml = checks.map(row => `${{fmtAgo(row.at_ms, Date.now())}}: ${{esc(row.detail || (row.ok ? 'ok' : 'issues'))}}`).join("<br/>");
        const exportsHtml = exports.map(row => `${{fmtAgo(row.at_ms, Date.now())}}: ${{esc(row.out_path || '-')}}`).join("<br/>");
        const packagesHtml = packageRows.map(row => `${{row.selected ? '<b>*</b> ' : ''}}<code>${{esc(row.physical_dir || '-')}}</code> ${{esc(row.version || '-') }} <span class="muted">(${{esc(row.source || '-')}})</span>`).join("<br/>");
        const journalHtml = journalRows.map(row => `${{esc(row.action || 'install')}} -> <code>${{esc(row.path || '-')}}</code>`).join("<br/>");
        if (detail) detail.innerHTML = `skill: <b>${{esc(body.name || '-')}}</b><br/>enabled: <b>${{body.enabled ? 'yes' : 'no'}}</b><br/>manifest/integrity/tests: <b>${{preflight.manifest_ok ? 'yes' : 'no'}}</b> / <b>${{preflight.integrity_ok ? 'yes' : 'no'}}</b> / <b>${{preflight.tests_present ? 'yes' : 'no'}}</b><br/>detail: <b>${{esc(preflight.detail || '-')}}</b><br/>package state: physical=<code>${{esc(packageState.selected_physical_dir || '-')}}</code>; mapped=<b>${{(packageState.mapped_directories || []).length || 0}}</b>; versioned=<b>${{packageState.versioned_package_count || 0}}</b>; journal pending=<b>${{packageState.journal_pending_count || 0}}</b><br/>package policy: preferred dir=<code>${{esc(packagePolicy.preferred_physical_dir || '-')}}</code>; preferred version=<b>${{esc(packagePolicy.preferred_version || '-')}}</b>; retention hours=<b>${{packagePolicy.retention_hours || 0}}</b>; restore export required=<b>${{packagePolicy.require_export_for_restore ? 'yes' : 'no'}}</b>; restore ready=<b>${{packagePolicy.restore_ready ? 'yes' : 'no'}}</b><br/>package policy warnings: ${{packageWarnings.length ? packageWarnings.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}<br/>latest export: <code>${{esc(packageState.latest_export_path || '-')}}</code><br/>package rows: ${{packagesHtml || '<span class="muted">none</span>'}}<br/>install journal: ${{journalHtml || '<span class="muted">none</span>'}}<br/>manifest errors: ${{manifestErrors.length ? manifestErrors.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}<br/>integrity errors: ${{integrityErrors.length ? integrityErrors.map(v => esc(v)).join('<br/>') : '<span class="muted">none</span>'}}<br/>recent actions: ${{actionsHtml || '<span class="muted">none</span>'}}<br/>recent checks: ${{checksHtml || '<span class="muted">none</span>'}}<br/>recent exports: ${{exportsHtml || '<span class="muted">none</span>'}}<br/>manifest:<pre style="white-space:pre-wrap; overflow:auto; background:#f8fafc; border:1px solid #d9e0ea; border-radius:8px; padding:8px; margin-top:6px;">${{esc(JSON.stringify(manifest, null, 2))}}</pre>`;
        if (status) status.textContent = `${{name}} detail loaded`;
        }} catch (err) {{
          if (status) status.textContent = `skill inspect failed: ${{err.message || err}}`;
        }}
      }}
      async function setSkillPackagePolicy(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{
          if (status) status.textContent = "API key required for skill package policy";
          return;
        }}
        if (status) status.textContent = `loading package policy for ${{name}}...`;
        try {{
          const detailResp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}`, {{
            headers: {{
              'X-API-Key': key
            }}
          }});
          const detailBody = await detailResp.json().catch(() => ({{}}));
          if (!detailResp.ok) {{
            throw new Error(detailBody.detail || detailBody.error || ('http ' + detailResp.status));
          }}
          const policy = detailBody.package_policy || {{}};
          const raw = window.prompt(
            `Edit package policy JSON for ${{name}}`,
            JSON.stringify({{
              preferred_physical_dir: policy.preferred_physical_dir || "",
              preferred_version: policy.preferred_version || "",
              retention_hours: policy.retention_hours || 24,
              require_export_for_restore: !!policy.require_export_for_restore
            }}, null, 2)
          );
          if (raw === null) {{
            if (status) status.textContent = "skill package policy update cancelled";
            return;
          }}
          let payload = null;
          try {{
            payload = JSON.parse(raw);
          }} catch (_err) {{
            if (status) status.textContent = "invalid JSON for skill package policy";
            return;
          }}
          if (status) status.textContent = `saving package policy for ${{name}}...`;
          const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/package-policy`, {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              'X-API-Key': key
            }},
            body: JSON.stringify(payload)
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) {{
            throw new Error(body.detail || body.error || ('http ' + resp.status));
          }}
          if (status) status.textContent = `${{name}} package policy updated`;
          await inspectSkill(name);
          await tick();
        }} catch (err) {{
          if (status) status.textContent = `skill package policy failed: ${{err.message || err}}`;
        }}
      }}
      async function uninstallSkill(name) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for skill uninstall";
        return;
      }}
      if (status) status.textContent = `uninstalling ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/skills/${{encodeURIComponent(name)}}/uninstall`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const detail = document.getElementById("skillsDetailPanel");
        if (detail) detail.innerHTML = "";
        if (status) status.textContent = `${{name}} uninstalled`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `skill uninstall failed: ${{err.message || err}}`;
      }}
    }}
    async function toggleChannel(name, enable) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for channel changes";
        return;
      }}
      if (status) status.textContent = `${{enable ? 'enabling' : 'disabling'}} channel ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/channels/${{encodeURIComponent(name)}}/${{enable ? 'enable' : 'disable'}}`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{name}} -> ${{enable ? 'enabled' : 'disabled'}} (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `channel action failed: ${{err.message || err}}`;
      }}
    }}
    async function acknowledgeChannelReload() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for apply confirmation";
        return;
      }}
      if (status) status.textContent = "recording channel apply confirmation...";
      try {{
        const resp = await fetch("/api/v1/channels/reload-ack", {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const runtime = body.runtime_execution || {{}};
        const runtimeNote = runtime.executed ? `; runtime -> ${{runtime.after_state || runtime.status || 'restarted'}}` : '';
        if (status) status.textContent = `channel apply confirmed (${{body.cleared_count || 0}} cleared; ${{body.pending_after || 0}} pending${{runtimeNote}})`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `apply confirmation failed: ${{err.message || err}}`;
      }}
    }}
    async function controlChannelRuntime(name, action) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for channel runtime control";
        return;
      }}
      if (status) status.textContent = `${{action}} runtime for ${{name}}...`;
      try {{
        const resp = await fetch(`/api/v1/channels/${{encodeURIComponent(name)}}/runtime/${{encodeURIComponent(action)}}`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const runtime = body.runtime || {{}};
        if (status) status.textContent = `${{name}} runtime -> ${{runtime.state || action}}`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `channel runtime control failed: ${{err.message || err}}`;
      }}
    }}
    async function loadChannelActionHistory() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("channelHistoryStatus");
      const result = document.getElementById("channelHistoryPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for channel history";
        return;
      }}
      const channelName = (document.getElementById("channelHistoryNameFilter")?.value || "").trim();
      const action = (document.getElementById("channelHistoryActionFilter")?.value || "").trim();
      const actor = (document.getElementById("channelHistoryActorFilter")?.value || "").trim();
      const limit = (document.getElementById("channelHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("channelHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "loading channel history...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (channelName) qs.set("channel_name", channelName);
        if (action) qs.set("action", action);
        if (actor) qs.set("actor", actor);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        const resp = await fetch(`/api/v1/channels/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const rowsHtml = rows.map(row => `<tr><td>${{fmtAgo(row.at_ms, Date.now())}}</td><td><code>${{esc(row.channel_name || '-')}}</code></td><td>${{esc(row.detail || row.action || '-')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
        if (result) result.innerHTML = `returned / total: <b>${{body.returned || 0}}</b> / <b>${{body.total || 0}}</b><br/><table><thead><tr><th>When</th><th>Channel</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="4" class="muted">No matching channel actions</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "channel history ready";
      }} catch (err) {{
        if (status) status.textContent = `channel history failed: ${{err.message || err}}`;
      }}
    }}
    async function exportChannelActionHistoryCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("channelHistoryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for channel history export";
        return;
      }}
      const channelName = (document.getElementById("channelHistoryNameFilter")?.value || "").trim();
      const action = (document.getElementById("channelHistoryActionFilter")?.value || "").trim();
      const actor = (document.getElementById("channelHistoryActorFilter")?.value || "").trim();
      const limit = (document.getElementById("channelHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("channelHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "exporting channel history CSV...";
      try {{
        const qs = new URLSearchParams();
        if (channelName) qs.set("channel_name", channelName);
        if (action) qs.set("action", action);
        if (actor) qs.set("actor", actor);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/channels/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "channel-action-history.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "channel history CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `channel history export failed: ${{err.message || err}}`;
      }}
    }}
    async function toggleAgentPlanning(agentId, enable) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent changes";
        return;
      }}
      if (status) status.textContent = `${{enable ? 'enabling' : 'disabling'}} planning for ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/planning/${{enable ? 'enable' : 'disable'}}`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} planning -> ${{enable ? 'enabled' : 'disabled'}} (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent action failed: ${{err.message || err}}`;
      }}
    }}
    async function updateAgentModel(agentId, currentModel) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent changes";
        return;
      }}
      const nextModel = (window.prompt(`Set model for ${{agentId}}`, currentModel || "") || "").trim();
      if (!nextModel) {{
        if (status) status.textContent = "agent model update cancelled";
        return;
      }}
      if (status) status.textContent = `updating model for ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/model`, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ model: nextModel }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} model -> ${{nextModel}} (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent model update failed: ${{err.message || err}}`;
      }}
    }}
    async function updateAgentModelPolicy(agentId, encodedCurrentPolicy) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent changes";
        return;
      }}
      let currentPolicy = {{}};
      try {{
        currentPolicy = JSON.parse(decodeURIComponent(encodedCurrentPolicy || "%7B%7D"));
      }} catch (_err) {{
        currentPolicy = {{}};
      }}
      const raw = window.prompt(
        `Set model policy JSON for ${{agentId}}`,
        JSON.stringify(currentPolicy, null, 2)
      );
      if (raw === null) {{
        if (status) status.textContent = "agent model policy update cancelled";
        return;
      }}
      let payload = null;
      try {{
        payload = JSON.parse(raw);
      }} catch (_err) {{
        if (status) status.textContent = "invalid JSON for agent model policy";
        return;
      }}
      if (status) status.textContent = `updating model policy for ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/model-policy`, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify(payload)
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} model policy updated (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent model policy update failed: ${{err.message || err}}`;
      }}
    }}
    async function updateAgentRoutingKeywords(agentId, currentKeywordsJson) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent changes";
        return;
      }}
      let currentKeywords = [];
      try {{
        currentKeywords = JSON.parse(decodeURIComponent(currentKeywordsJson || "%5B%5D"));
      }} catch (_err) {{
        currentKeywords = [];
      }}
      const initial = Array.isArray(currentKeywords) ? currentKeywords.join(", ") : "";
      const raw = window.prompt(`Set routing keywords for ${{agentId}} (comma separated)`, initial);
      if (raw === null) {{
        if (status) status.textContent = "agent routing update cancelled";
        return;
      }}
      const routingKeywords = raw.split(",").map(v => v.trim()).filter(Boolean);
      if (status) status.textContent = `updating routing keywords for ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/routing-keywords`, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ routing_keywords: routingKeywords }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} routing keywords updated (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent routing update failed: ${{err.message || err}}`;
      }}
    }}
    async function createAgentProfile() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent profile changes";
        return;
      }}
      const agentId = (window.prompt("Create agent profile id", "") || "").trim();
      if (!agentId) {{
        if (status) status.textContent = "agent profile create cancelled";
        return;
      }}
      const raw = window.prompt(
        `Set profile JSON for ${{agentId}}`,
        JSON.stringify({{ display_name: agentId, model: "", workspace: "", routing_keywords: [], skill_names: [] }}, null, 2)
      );
      if (raw === null) {{
        if (status) status.textContent = "agent profile create cancelled";
        return;
      }}
      let payload = null;
      try {{
        payload = JSON.parse(raw);
      }} catch (_err) {{
        if (status) status.textContent = "invalid JSON for agent profile";
        return;
      }}
      if (status) status.textContent = `creating profile ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/profile`, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ profile: payload }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} profile created (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent profile create failed: ${{err.message || err}}`;
      }}
    }}
    async function saveAgentProfile(agentId) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent profile changes";
        return;
      }}
      if (status) status.textContent = `loading profile ${{agentId}}...`;
      try {{
        const detailResp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const detailBody = await detailResp.json().catch(() => ({{}}));
        if (!detailResp.ok) {{
          throw new Error(detailBody.detail || detailBody.error || ('http ' + detailResp.status));
        }}
        const raw = window.prompt(
          `Edit profile JSON for ${{agentId}}`,
          JSON.stringify(detailBody.profile_overrides || {{}}, null, 2)
        );
        if (raw === null) {{
          if (status) status.textContent = "agent profile update cancelled";
          return;
        }}
        let payload = null;
        try {{
          payload = JSON.parse(raw);
        }} catch (_err) {{
          if (status) status.textContent = "invalid JSON for agent profile";
          return;
        }}
        if (status) status.textContent = `saving profile ${{agentId}}...`;
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}/profile`, {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{ profile: payload }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} profile saved (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent profile update failed: ${{err.message || err}}`;
      }}
    }}
    async function deleteAgentProfile(agentId) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent profile changes";
        return;
      }}
      if (!window.confirm(`Delete profile ${{agentId}}?`)) {{
        if (status) status.textContent = "agent profile delete cancelled";
        return;
      }}
      if (status) status.textContent = `deleting profile ${{agentId}}...`;
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}`, {{
          method: 'DELETE',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{agentId}} profile deleted (reload required)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent profile delete failed: ${{err.message || err}}`;
      }}
    }}
    async function previewAgentRoute() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentRoutePreviewStatus");
      const result = document.getElementById("agentRoutePreviewResult");
      if (!key) {{
        if (status) status.textContent = "API key required for route preview";
        return;
      }}
      const channel = (document.getElementById("agentRoutePreviewChannel")?.value || "").trim();
      const chatId = (document.getElementById("agentRoutePreviewChatId")?.value || "").trim();
      const userId = (document.getElementById("agentRoutePreviewUserId")?.value || "").trim();
      const content = (document.getElementById("agentRoutePreviewContent")?.value || "").trim();
      const explicitAgentId = (document.getElementById("agentRoutePreviewExplicitAgentId")?.value || "").trim();
      if (!channel || !chatId || !userId) {{
        if (status) status.textContent = "channel / chat_id / user_id are required";
        return;
      }}
      if (status) status.textContent = "previewing route...";
      if (result) result.innerHTML = "";
      try {{
        const resp = await fetch("/api/v1/agents/route-preview", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            channel,
            chat_id: chatId,
            user_id: userId,
            content,
            explicit_agent_id: explicitAgentId
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const decision = body.decision || {{}};
        const routeAudit = Array.isArray(body.route_audit) ? body.route_audit.slice(-3) : [];
        const auditHtml = routeAudit.map(row => `${{esc(row.previous_agent_id || '-')}} -> <b>${{esc(row.new_agent_id || '-')}}</b> <span class="muted">(${{esc(row.reason || '-')}})</span>`).join("<br/>");
        if (result) result.innerHTML = `decision: <b>${{esc(decision.agent_id || '-')}}</b> via <b>${{esc(decision.reason || '-')}}</b><br/>matched keyword: <b>${{esc(decision.matched_keyword || '-')}}</b><br/>bound agent: <b>${{esc(body.bound_agent_id || '-')}}</b><br/>channel default: <b>${{esc(body.channel_profile || '-')}}</b><br/>registered: <b>${{decision.registered ? 'yes' : 'no'}}</b><br/>recent route audit: ${{auditHtml || '<span class="muted">none</span>'}}`;
        if (status) status.textContent = "route preview ready";
      }} catch (err) {{
        if (status) status.textContent = `route preview failed: ${{err.message || err}}`;
      }}
    }}
    async function bindAgentRoute() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentRoutePreviewStatus");
      const channel = (document.getElementById("agentRoutePreviewChannel")?.value || "").trim();
      const chatId = (document.getElementById("agentRoutePreviewChatId")?.value || "").trim();
      const userId = (document.getElementById("agentRoutePreviewUserId")?.value || "").trim();
      const agentId = (document.getElementById("agentRoutePreviewExplicitAgentId")?.value || "").trim();
      if (!key) {{
        if (status) status.textContent = "API key required for route binding";
        return;
      }}
      if (!channel || !chatId || !userId || !agentId) {{
        if (status) status.textContent = "channel / chat_id / user_id / explicit agent are required";
        return;
      }}
      if (status) status.textContent = "binding sticky route...";
      try {{
        const resp = await fetch("/api/v1/agents/route-bind", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            channel,
            chat_id: chatId,
            user_id: userId,
            agent_id: agentId
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `route bound to ${{body.bound_agent_id || agentId}}`;
        await previewAgentRoute();
      }} catch (err) {{
        if (status) status.textContent = `route bind failed: ${{err.message || err}}`;
      }}
    }}
    async function clearAgentRoute() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentRoutePreviewStatus");
      const channel = (document.getElementById("agentRoutePreviewChannel")?.value || "").trim();
      const chatId = (document.getElementById("agentRoutePreviewChatId")?.value || "").trim();
      const userId = (document.getElementById("agentRoutePreviewUserId")?.value || "").trim();
      if (!key) {{
        if (status) status.textContent = "API key required for route clear";
        return;
      }}
      if (!channel || !chatId || !userId) {{
        if (status) status.textContent = "channel / chat_id / user_id are required";
        return;
      }}
      if (status) status.textContent = "clearing sticky route...";
      try {{
        const resp = await fetch("/api/v1/agents/route-clear", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            channel,
            chat_id: chatId,
            user_id: userId
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `route cleared${{body.previous_agent_id ? ` (was ${{body.previous_agent_id}})` : ''}}`;
        await previewAgentRoute();
      }} catch (err) {{
        if (status) status.textContent = `route clear failed: ${{err.message || err}}`;
      }}
    }}
    async function inspectAgentProfile(agentId) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentRoutePreviewStatus");
      const result = document.getElementById("agentRoutePreviewResult");
      if (!key) {{
        if (status) status.textContent = "API key required for agent inspection";
        return;
      }}
      if (status) status.textContent = `loading profile details for ${{agentId}}...`;
      if (result) result.innerHTML = "";
      try {{
        const resp = await fetch(`/api/v1/agents/${{encodeURIComponent(agentId)}}`, {{
          method: 'GET',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const effective = body.effective || {{}};
        const refs = Array.isArray(body.channel_references) ? body.channel_references : [];
        const profileOverrides = body.profile_overrides || {{}};
        const promptSummary = effective.system_prompt ? `${{effective.system_prompt.length}} chars inline` : (effective.system_prompt_file ? `file: ${{effective.system_prompt_file}}` : 'none');
        const refsHtml = refs.map(row => `<code>${{esc(row.channel || '-')}}</code>`).join(", ");
        if (result) result.innerHTML = `profile: <b>${{esc(body.display_name || body.agent_id || '-')}}</b> <span class="muted">(<code>${{esc(body.agent_id || '-')}}</code>)</span><br/>workspace: <code>${{esc(effective.workspace || '-')}}</code><br/>model stack: <b>${{esc(effective.model || '-')}}</b> / vision=${{esc(effective.vision_model || '-')}} / thinking=${{esc(effective.thinking_model || '-')}} / fallback=${{esc(effective.fallback_model || '-')}} / cost=${{esc(effective.cost_model || '-')}} / stability=${{esc(effective.stability_model || '-')}}<br/>dynamic overrides: intent=${{Object.keys(effective.intent_model_overrides || {{}}).length}} / task-type=${{Object.keys(effective.task_type_model_overrides || {{}}).length}} / allow-list=${{(effective.allowed_models || []).length}}<br/>planning: <b>${{effective.planning_enabled ? 'enabled' : 'disabled'}}</b>; memory: <b>${{esc(effective.memory_recall_mode || '-')}}</b>; max tool iterations: <b>${{effective.max_tool_iterations || 0}}</b><br/>prompt binding: <b>${{esc(promptSummary)}}</b><br/>tool binding: allow=${{(effective.allowed_tools || []).length}} deny=${{(effective.denied_tools || []).length}}<br/>skill preload: <b>${{(effective.skill_names || []).length ? esc((effective.skill_names || []).join(', ')) : 'none'}}</b><br/>channel references: ${{refsHtml || '<span class="muted">none</span>'}}<br/>profile overrides:<pre style="white-space:pre-wrap; overflow:auto; background:#f8fafc; border:1px solid #d9e0ea; border-radius:8px; padding:8px; margin-top:6px;">${{esc(JSON.stringify(profileOverrides, null, 2))}}</pre>`;
        if (status) status.textContent = `${{agentId}} profile detail loaded`;
      }} catch (err) {{
        if (status) status.textContent = `agent inspect failed: ${{err.message || err}}`;
      }}
    }}
    async function acknowledgeAgentReload() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentRoutePreviewStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for apply confirmation";
        return;
      }}
      if (status) status.textContent = "recording agent apply confirmation...";
      try {{
        const resp = await fetch("/api/v1/agents/reload-ack", {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
          if (status) status.textContent = `agent apply confirmed (${{body.cleared_count || 0}} cleared; ${{body.pending_after || 0}} pending)`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `agent apply confirmation failed: ${{err.message || err}}`;
      }}
    }}
    async function loadAgentActionHistory() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentHistoryStatus");
      const result = document.getElementById("agentHistoryPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for agent history";
        return;
      }}
      const agentId = (document.getElementById("agentHistoryAgentFilter")?.value || "").trim();
      const action = (document.getElementById("agentHistoryActionFilter")?.value || "").trim();
      const actor = (document.getElementById("agentHistoryActorFilter")?.value || "").trim();
      const limit = (document.getElementById("agentHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("agentHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "loading agent history...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (agentId) qs.set("agent_id", agentId);
        if (action) qs.set("action", action);
        if (actor) qs.set("actor", actor);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        const resp = await fetch(`/api/v1/agents/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const rowsHtml = rows.map(row => `<tr><td>${{fmtAgo(row.at_ms, Date.now())}}</td><td><code>${{esc(row.agent_id || '-')}}</code></td><td>${{esc(row.detail || row.action || '-')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
        if (result) result.innerHTML = `returned / total: <b>${{body.returned || 0}}</b> / <b>${{body.total || 0}}</b><br/><table><thead><tr><th>When</th><th>Agent</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="4" class="muted">No matching agent actions</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "agent history ready";
      }} catch (err) {{
        if (status) status.textContent = `agent history failed: ${{err.message || err}}`;
      }}
    }}
    async function exportAgentActionHistoryCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("agentHistoryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for agent history export";
        return;
      }}
      const agentId = (document.getElementById("agentHistoryAgentFilter")?.value || "").trim();
      const action = (document.getElementById("agentHistoryActionFilter")?.value || "").trim();
      const actor = (document.getElementById("agentHistoryActorFilter")?.value || "").trim();
      const limit = (document.getElementById("agentHistoryLimit")?.value || "10").trim();
      const offset = (document.getElementById("agentHistoryOffset")?.value || "0").trim();
      if (status) status.textContent = "exporting agent history CSV...";
      try {{
        const qs = new URLSearchParams();
        if (agentId) qs.set("agent_id", agentId);
        if (action) qs.set("action", action);
        if (actor) qs.set("actor", actor);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/agents/history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "agent-action-history.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "agent history CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `agent history export failed: ${{err.message || err}}`;
      }}
    }}
    function exportModelUsage() {{
      const data = window.__snap?.agent?.model_routing_events || [];
      const modelFilter = (document.getElementById("modelUsageModelFilter")?.value || "").trim().toLowerCase();
      const reasonFilter = (document.getElementById("modelUsageReasonFilter")?.value || "").trim().toLowerCase();
      const sinceHours = Number((document.getElementById("modelUsageSinceHours")?.value || "0").trim() || 0);
      const fromAtMs = Number((document.getElementById("modelUsageFromAtMs")?.value || "0").trim() || 0);
      const toAtMs = Number((document.getElementById("modelUsageToAtMs")?.value || "0").trim() || 0);
      const now = Date.now();
      const filtered = data.filter(row => {{
        const okModel = !modelFilter || String(row.selected_model || "").toLowerCase() === modelFilter;
        const okReason = !reasonFilter || String(row.reason || "").toLowerCase() === reasonFilter;
        const okSince = !sinceHours || Number(row.at_ms || 0) >= (now - (sinceHours * 3600 * 1000));
        const okFrom = !fromAtMs || Number(row.at_ms || 0) >= fromAtMs;
        const okTo = !toAtMs || Number(row.at_ms || 0) <= toAtMs;
        return okModel && okReason && okSince && okFrom && okTo;
      }});
      const blob = new Blob([JSON.stringify(filtered, null, 2)], {{ type: "application/json;charset=utf-8" }});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "model-routing-events.json";
      anchor.click();
      URL.revokeObjectURL(url);
    }}
    async function exportModelUsageCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("skillsActionStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for model routing export";
        return;
      }}
      const modelFilter = (document.getElementById("modelUsageModelFilter")?.value || "").trim();
      const reasonFilter = (document.getElementById("modelUsageReasonFilter")?.value || "").trim();
      const limit = (document.getElementById("modelUsageLimit")?.value || "20").trim();
      const offset = (document.getElementById("modelUsageOffset")?.value || "0").trim();
      const sinceHours = (document.getElementById("modelUsageSinceHours")?.value || "0").trim();
      const fromAtMs = (document.getElementById("modelUsageFromAtMs")?.value || "0").trim();
      const toAtMs = (document.getElementById("modelUsageToAtMs")?.value || "0").trim();
      if (status) status.textContent = "exporting model routing CSV...";
      try {{
        const qs = new URLSearchParams();
        if (modelFilter) qs.set("model", modelFilter);
        if (reasonFilter) qs.set("reason", reasonFilter);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        if (sinceHours) qs.set("since_hours", sinceHours);
        if (fromAtMs) qs.set("from_at_ms", fromAtMs);
        if (toAtMs) qs.set("to_at_ms", toAtMs);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/model-routing?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "model-routing-events.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "model routing CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `model routing CSV export failed: ${{err.message || err}}`;
      }}
    }}
    async function exportOpsSummary(format) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("opsSummaryStatus");
      const fmt = String(format || "json").trim().toLowerCase() || "json";
      if (!key) {{
        if (status) status.textContent = "API key required for operations summary export";
        return;
      }}
      if (status) status.textContent = `exporting operations summary ${{fmt}}...`;
      try {{
        const qs = buildOpsSummaryQuery(format === "csv" ? "csv" : fmt);
        const resp = await fetch(`/api/v1/ops/summary?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = fmt === "csv"
          ? await resp.blob()
          : new Blob([JSON.stringify(await resp.json(), null, 2)], {{ type: "application/json;charset=utf-8" }});
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = fmt === "csv" ? "operations-summary.csv" : "operations-summary.json";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = `operations summary ${{fmt}} exported`;
      }} catch (err) {{
        if (status) status.textContent = `operations summary export failed: ${{err.message || err}}`;
      }}
    }}
    function buildOpsSummaryQuery(formatOverride) {{
      const qs = new URLSearchParams();
      const surface = (document.getElementById("opsSurfaceFilter")?.value || "").trim();
      const actor = (document.getElementById("opsActorFilter")?.value || "").trim();
      const target = (document.getElementById("opsTargetFilter")?.value || "").trim();
      const pendingOnly = !!document.getElementById("opsPendingOnly")?.checked;
      const limit = (document.getElementById("opsLimit")?.value || "10").trim();
      const offset = (document.getElementById("opsOffset")?.value || "0").trim();
      const fmt = String(formatOverride || "").trim().toLowerCase();
      if (surface) qs.set("surface", surface);
      if (actor) qs.set("actor", actor);
      if (target) qs.set("target", target);
      if (pendingOnly) qs.set("pending_only", "true");
      if (limit) qs.set("limit", limit);
      if (offset) qs.set("offset", offset);
      if (fmt) qs.set("format", fmt);
      return qs;
    }}
    async function loadOpsSummary() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("opsSummaryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for filtered operations summary";
        return;
      }}
      if (status) status.textContent = "loading filtered operations summary...";
      try {{
        const qs = buildOpsSummaryQuery("");
        const resp = await fetch(`/api/v1/ops/summary?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        window.__opsReport = body;
        if (window.__snap) renderOps(window.__snap, body);
        if (status) status.textContent = "filtered operations summary ready";
      }} catch (err) {{
        if (status) status.textContent = `filtered operations summary failed: ${{err.message || err}}`;
      }}
    }}
    async function previewPendingOps(surface, target) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("opsSummaryStatus");
      const result = document.getElementById("opsApplyPlanPanel");
      const explicitSurface = String(surface || "").trim().toLowerCase();
      const explicitTarget = String(target || "").trim();
      const query = buildOpsSummaryQuery("");
      const targetSurface = explicitSurface || (query.get("surface") || "all");
      const targetActor = (query.get("actor") || "").trim();
      const targetName = explicitTarget || (query.get("target") || "").trim();
      if (!key) {{
        if (status) status.textContent = "API key required for apply preview";
        return;
      }}
      if (status) status.textContent = "loading apply preview...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (targetSurface && targetSurface !== "all") qs.set("surface", targetSurface);
        if (targetActor) qs.set("actor", targetActor);
        if (targetName) qs.set("target", targetName);
        const resp = await fetch(`/api/v1/ops/apply-plan?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const steps = Array.isArray(body.steps) ? body.steps : [];
        const summaryRows = Array.isArray(body.surface_summary) ? body.surface_summary : [];
        const summaryHtml = summaryRows.map(row => `<tr><td>${{esc(row.surface || '-')}}</td><td>${{row.target_count || 0}}</td><td>${{row.runtime_apply_target_count || 0}}</td><td>${{row.audit_only_target_count || 0}}</td><td>${{row.unsupported_target_count || 0}}</td><td><code>${{esc((row.targets || []).join(', ') || '-')}}</code></td></tr>`).join('');
          const stepsHtml = steps.map(row => `<tr><td>${{row.step || 0}}</td><td>${{esc(row.surface || '-')}}</td><td><code>${{esc(row.target || '-')}}</code></td><td>${{esc(row.action || '-')}}</td><td>${{esc(row.execution_mode || '-')}}</td><td>${{esc(row.capability_status || '-')}}</td></tr>`).join('');
          const rowsHtml = rows.map(row => `<tr><td>${{esc(row.surface || '-')}}</td><td><code>${{esc(row.target || '-')}}</code></td><td>${{esc(row.action || '-')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
            if (result) result.innerHTML = `matched / apply targets: <b>${{body.matched_pending_total || 0}}</b> / <b>${{body.apply_target_total || 0}}</b><br/><table><thead><tr><th>Surface</th><th>Targets</th><th>Exec</th><th>Audit-only</th><th>Unsupported</th><th>Target List</th></tr></thead><tbody>${{summaryHtml || '<tr><td colspan="6" class="muted">No grouped apply targets</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Step</th><th>Surface</th><th>Target</th><th>Action</th><th>Mode</th><th>Capability</th></tr></thead><tbody>${{stepsHtml || '<tr><td colspan="6" class="muted">No apply steps in scope</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Surface</th><th>Target</th><th>Action</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="4" class="muted">No pending apply actions in scope</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = `apply preview ready: ${{body.apply_target_total || 0}} target(s)`;
      }} catch (err) {{
        if (status) status.textContent = `apply preview failed: ${{err.message || err}}`;
      }}
    }}
    async function applyPendingOps(surface, target) {{
      const key = getSkillsApiKey();
      const status = document.getElementById("opsSummaryStatus");
      const explicitSurface = String(surface || "").trim().toLowerCase();
      const explicitTarget = String(target || "").trim();
      const query = buildOpsSummaryQuery("");
      const targetSurface = explicitSurface || (query.get("surface") || "all");
      const targetActor = (query.get("actor") || "").trim();
      const targetName = explicitTarget || (query.get("target") || "").trim();
      if (!key) {{
        if (status) status.textContent = "API key required for pending apply acknowledgement";
        return;
      }}
      if (status) status.textContent = `marking pending ${'{'}targetSurface{'}'}${'{'}targetName ? ':' + targetName : ''{'}'} as applied...`;
      try {{
        const qs = new URLSearchParams();
        if (targetSurface && targetSurface !== "all") qs.set("surface", targetSurface);
        if (targetActor) qs.set("actor", targetActor);
        if (targetName) qs.set("target", targetName);
        const resp = await fetch(`/api/v1/ops/apply-pending?${{qs.toString()}}`, {{
          method: 'POST',
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const result = document.getElementById("opsApplyPlanPanel");
        const surfaceSummary = Array.isArray(body.surface_summary) ? body.surface_summary : [];
        const steps = Array.isArray(body.steps) ? body.steps : [];
        const summaryHtml = surfaceSummary.map(row => `<tr><td>${{esc(row.surface || '-')}}</td><td>${{row.target_count || 0}}</td><td>${{row.runtime_apply_target_count || 0}}</td><td>${{row.audit_only_target_count || 0}}</td><td>${{row.unsupported_target_count || 0}}</td><td>${{esc((Array.isArray(row.targets) ? row.targets : []).join(', ') || '-')}}</td></tr>`).join('');
          const stepsHtml = steps.map(row => `<tr><td>${{row.step || 0}}</td><td>${{esc(row.surface || '-')}}</td><td><code>${{esc(row.target || '-')}}</code></td><td>${{esc(row.action || '-')}}</td><td>${{esc(row.execution_mode || '-')}}</td><td>${{esc(row.capability_status || '-')}}</td><td>${{esc(row.status || '-')}}</td><td>${{esc(row.result || '-')}}</td></tr>`).join('');
            if (result) result.innerHTML = `applied / cleared / remaining: <b>${{body.applied_count || 0}}</b> / <b>${{body.cleared_count || 0}}</b> / <b>${{body.pending_after || 0}}</b><br/><table><thead><tr><th>Surface</th><th>Targets</th><th>Exec</th><th>Audit-only</th><th>Unsupported</th><th>Target List</th></tr></thead><tbody>${{summaryHtml || '<tr><td colspan="6" class="muted">No grouped apply results</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Step</th><th>Surface</th><th>Target</th><th>Action</th><th>Mode</th><th>Capability</th><th>Status</th><th>Result</th></tr></thead><tbody>${{stepsHtml || '<tr><td colspan="8" class="muted">No apply execution steps</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = `marked ${{body.applied_count || 0}} apply acknowledgement(s); cleared ${{body.cleared_count || 0}}; remaining ${{body.pending_after || 0}}`;
        await tick();
        await loadOpsSummary();
      }} catch (err) {{
        if (status) status.textContent = `pending apply acknowledgement failed: ${{err.message || err}}`;
      }}
    }}
    function rerenderModelUsage() {{
      if (window.__snap) renderModelUsage(window.__snap);
    }}
    async function updateTenantBackendPolicy() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("tenantPolicyStatus");
      const tenantId = (document.getElementById("tenantPolicyTenantId")?.value || "").trim();
      const backend = (document.getElementById("tenantPolicyBackend")?.value || "").trim();
      if (!key) {{
        if (status) status.textContent = "API key required for tenant policy changes";
        return;
      }}
      if (!tenantId) {{
        if (status) status.textContent = "tenant_id is required";
        return;
      }}
      if (status) status.textContent = `updating ${{tenantId}}...`;
      try {{
        const resp = await fetch("/api/v1/rag/tenant-policy", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            tenant_id: tenantId,
            store_backend: backend
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{tenantId}} -> ${{backend || 'default'}}`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `tenant policy failed: ${{err.message || err}}`;
      }}
    }}
    async function updateNotebookBackendPolicy() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("notebookPolicyStatus");
      const tenantId = (document.getElementById("notebookPolicyTenantId")?.value || "").trim();
      const notebookId = (document.getElementById("notebookPolicyNotebookId")?.value || "").trim();
      const backend = (document.getElementById("notebookPolicyBackend")?.value || "").trim();
      const retentionMaxDocuments = Number((document.getElementById("notebookPolicyRetentionMaxDocuments")?.value || "0").trim() || 0);
      const retentionMaxAgeDays = Number((document.getElementById("notebookPolicyRetentionMaxAgeDays")?.value || "0").trim() || 0);
      if (!key) {{
        if (status) status.textContent = "API key required for notebook policy changes";
        return;
      }}
      if (!tenantId || !notebookId) {{
        if (status) status.textContent = "tenant_id and notebook_id are required";
        return;
      }}
      if (status) status.textContent = `updating ${{tenantId}}/${{notebookId}}...`;
      try {{
        const resp = await fetch("/api/v1/rag/notebook-policy", {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-API-Key': key
          }},
          body: JSON.stringify({{
            tenant_id: tenantId,
            notebook_id: notebookId,
            store_backend: backend,
            retention_max_documents: retentionMaxDocuments,
            retention_max_age_days: retentionMaxAgeDays
          }})
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        if (status) status.textContent = `${{tenantId}}/${{notebookId}} -> ${{backend || 'default'}}; retention=${{body.retention_max_documents || 0}} docs / ${{body.retention_max_age_days || 0}} days`;
        await tick();
      }} catch (err) {{
        if (status) status.textContent = `notebook policy failed: ${{err.message || err}}`;
      }}
    }}
    async function loadKnowledgePolicyHistory() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("knowledgePolicyHistoryStatus");
      const result = document.getElementById("knowledgePolicyHistoryPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for policy history";
        return;
      }}
      const policyKind = (document.getElementById("knowledgePolicyKindFilter")?.value || "").trim();
      const actor = (document.getElementById("knowledgePolicyActorFilter")?.value || "").trim();
      const tenantId = (document.getElementById("knowledgePolicyTenantFilter")?.value || "").trim();
      const targetId = (document.getElementById("knowledgePolicyTargetFilter")?.value || "").trim();
      const beforeBackend = (document.getElementById("knowledgePolicyBeforeBackendFilter")?.value || "").trim();
      const afterBackend = (document.getElementById("knowledgePolicyAfterBackendFilter")?.value || "").trim();
      const limit = (document.getElementById("knowledgePolicyLimit")?.value || "10").trim();
      const offset = (document.getElementById("knowledgePolicyOffset")?.value || "0").trim();
      if (status) status.textContent = "loading policy history...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (policyKind) qs.set("policy_kind", policyKind);
        if (actor) qs.set("actor", actor);
        if (tenantId) qs.set("tenant_id", tenantId);
        if (targetId) qs.set("target_id", targetId);
        if (beforeBackend) qs.set("before_store_backend", beforeBackend);
        if (afterBackend) qs.set("after_store_backend", afterBackend);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        const resp = await fetch(`/api/v1/rag/policy-history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const rowsHtml = rows.map(row => `<tr><td>${{fmtAgo(row.at_ms, Date.now())}}</td><td>${{esc(row.policy_kind || '-')}}</td><td><code>${{esc(row.tenant_id || '-')}}</code></td><td><code>${{esc(row.target_id || '-')}}</code></td><td>${{esc(row.before_store_backend || 'default')}} -> ${{esc(row.after_store_backend || 'default')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
        if (result) result.innerHTML = `returned / total: <b>${{body.returned || 0}}</b> / <b>${{body.total || 0}}</b><br/><table><thead><tr><th>When</th><th>Kind</th><th>Tenant</th><th>Target</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="6" class="muted">No matching policy changes</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "policy history ready";
      }} catch (err) {{
        if (status) status.textContent = `policy history failed: ${{err.message || err}}`;
      }}
    }}
    async function exportKnowledgePolicyHistoryCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("knowledgePolicyHistoryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for policy history export";
        return;
      }}
      const policyKind = (document.getElementById("knowledgePolicyKindFilter")?.value || "").trim();
      const actor = (document.getElementById("knowledgePolicyActorFilter")?.value || "").trim();
      const tenantId = (document.getElementById("knowledgePolicyTenantFilter")?.value || "").trim();
      const targetId = (document.getElementById("knowledgePolicyTargetFilter")?.value || "").trim();
      const beforeBackend = (document.getElementById("knowledgePolicyBeforeBackendFilter")?.value || "").trim();
      const afterBackend = (document.getElementById("knowledgePolicyAfterBackendFilter")?.value || "").trim();
      const limit = (document.getElementById("knowledgePolicyLimit")?.value || "10").trim();
      const offset = (document.getElementById("knowledgePolicyOffset")?.value || "0").trim();
      if (status) status.textContent = "exporting policy history CSV...";
      try {{
        const qs = new URLSearchParams();
        if (policyKind) qs.set("policy_kind", policyKind);
        if (actor) qs.set("actor", actor);
        if (tenantId) qs.set("tenant_id", tenantId);
        if (targetId) qs.set("target_id", targetId);
        if (beforeBackend) qs.set("before_store_backend", beforeBackend);
        if (afterBackend) qs.set("after_store_backend", afterBackend);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/rag/policy-history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "knowledge-policy-history.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "policy history CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `policy history export failed: ${{err.message || err}}`;
      }}
    }}
    async function loadKnowledgeActivityHistory() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("knowledgeActivityHistoryStatus");
      const result = document.getElementById("knowledgeActivityHistoryPanel");
      if (!key) {{
        if (status) status.textContent = "API key required for knowledge activity";
        return;
      }}
      const eventKind = (document.getElementById("knowledgeActivityKindFilter")?.value || "").trim();
      const tenantId = (document.getElementById("knowledgeActivityTenantFilter")?.value || "").trim();
      const actor = (document.getElementById("knowledgeActivityActorFilter")?.value || "").trim();
      const eventStatus = (document.getElementById("knowledgeActivityStatusFilter")?.value || "").trim();
      const limit = (document.getElementById("knowledgeActivityLimit")?.value || "10").trim();
      const offset = (document.getElementById("knowledgeActivityOffset")?.value || "0").trim();
      if (status) status.textContent = "loading knowledge activity...";
      if (result) result.innerHTML = "";
      try {{
        const qs = new URLSearchParams();
        if (eventKind) qs.set("event_kind", eventKind);
        if (tenantId) qs.set("tenant_id", tenantId);
        if (actor) qs.set("actor", actor);
        if (eventStatus) qs.set("status", eventStatus);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        const resp = await fetch(`/api/v1/rag/activity-history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        const body = await resp.json().catch(() => ({{}}));
        if (!resp.ok) {{
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const rows = Array.isArray(body.rows) ? body.rows : [];
        const rowsHtml = rows.map(row => `<tr><td>${{fmtAgo(row.at_ms, Date.now())}}</td><td>${{esc(row.event_kind || '-')}}</td><td><code>${{esc(row.tenant_id || '-')}}</code></td><td><code>${{esc(row.target_id || '-')}}</code></td><td>${{esc(row.detail || '-')}}</td><td>${{esc(row.status || '-')}}</td><td>${{esc(row.actor || '-')}}</td></tr>`).join('');
        if (result) result.innerHTML = `returned / total: <b>${{body.returned || 0}}</b> / <b>${{body.total || 0}}</b><br/><table><thead><tr><th>When</th><th>Kind</th><th>Tenant</th><th>Target</th><th>Detail</th><th>Status</th><th>Actor</th></tr></thead><tbody>${{rowsHtml || '<tr><td colspan="7" class="muted">No matching knowledge activity</td></tr>'}}</tbody></table>`;
        if (status) status.textContent = "knowledge activity ready";
      }} catch (err) {{
        if (status) status.textContent = `knowledge activity failed: ${{err.message || err}}`;
      }}
    }}
    async function exportKnowledgeActivityHistoryCsv() {{
      const key = getSkillsApiKey();
      const status = document.getElementById("knowledgeActivityHistoryStatus");
      if (!key) {{
        if (status) status.textContent = "API key required for knowledge activity export";
        return;
      }}
      const eventKind = (document.getElementById("knowledgeActivityKindFilter")?.value || "").trim();
      const tenantId = (document.getElementById("knowledgeActivityTenantFilter")?.value || "").trim();
      const actor = (document.getElementById("knowledgeActivityActorFilter")?.value || "").trim();
      const eventStatus = (document.getElementById("knowledgeActivityStatusFilter")?.value || "").trim();
      const limit = (document.getElementById("knowledgeActivityLimit")?.value || "10").trim();
      const offset = (document.getElementById("knowledgeActivityOffset")?.value || "0").trim();
      if (status) status.textContent = "exporting knowledge activity CSV...";
      try {{
        const qs = new URLSearchParams();
        if (eventKind) qs.set("event_kind", eventKind);
        if (tenantId) qs.set("tenant_id", tenantId);
        if (actor) qs.set("actor", actor);
        if (eventStatus) qs.set("status", eventStatus);
        if (limit) qs.set("limit", limit);
        if (offset) qs.set("offset", offset);
        qs.set("format", "csv");
        const resp = await fetch(`/api/v1/rag/activity-history?${{qs.toString()}}`, {{
          headers: {{
            'X-API-Key': key
          }}
        }});
        if (!resp.ok) {{
          const body = await resp.json().catch(() => ({{}}));
          throw new Error(body.detail || body.error || ('http ' + resp.status));
        }}
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "knowledge-activity-history.csv";
        anchor.click();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "knowledge activity CSV exported";
      }} catch (err) {{
        if (status) status.textContent = `knowledge activity export failed: ${{err.message || err}}`;
      }}
    }}
    function badge(v) {{
      if (v === true) return '<span class="ok">true</span>';
      if (v === false) return '<span class="bad">false</span>';
      return String(v);
    }}
    function fmtAgo(ms, now) {{
      if (!ms) return '-';
      const sec = Math.max(0, Math.floor((now - Number(ms)) / 1000));
      if (sec < 60) return sec + 's ago';
      if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
      if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
      return Math.floor(sec / 86400) + 'd ago';
    }}
    function describeExecutionTrace(summary) {{
      const data = summary || {{}};
      const parts = [];
      if (data.reflections_used) parts.push(`reflections=${{data.reflections_used}}`);
      if (data.tool_failures) parts.push(`tool_failures=${{data.tool_failures}}`);
      if (data.fallback_used) parts.push(`fallback=${{data.fallback_reason || 'used'}}`);
      if (data.reload_triggered) parts.push('reload=true');
      if (data.approval_waited) parts.push('approval_waited=true');
      return parts.join('; ') || '-';
    }}
    function renderNodeDetails(data) {{
      const now = Date.now();
      const rows = (data.node.nodes || []).slice(0, 20).map(n => `
        <tr>
          <td>${{n.name || '-'}}</td>
          <td>${{n.platform || '-'}}</td>
          <td>${{n.status || '-'}}</td>
          <td>${{fmtAgo(n.last_seen_ms, now)}}</td>
          <td>${{n.allow_gateway_tasks ? 'yes' : 'no'}} / ${{n.max_running_tasks}}</td>
          <td>${{n.approval_required_count}}</td>
          <td>${{n.latest_task_type || '-'}} / ${{n.latest_task_status || '-'}}</td>
        </tr>`).join('');
      p("nodeDetails", `<table>
        <thead><tr><th>Node</th><th>Platform</th><th>Status</th><th>Last Seen</th><th>Gateway/MaxRun</th><th>Approvals</th><th>Latest Task</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="7" class="muted">No nodes</td></tr>'}}</tbody>
      </table>`);
    }}
    function renderApprovalTimeline(data) {{
      const now = Date.now();
      const rows = (data.node.approval_timeline || []).slice(0, 20).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, now)}}</td>
          <td>${{e.action || '-'}}</td>
          <td>${{e.node_id || '-'}}</td>
          <td>${{e.task_id || '-'}}</td>
          <td>${{e.actor || '-'}}</td>
          <td>${{e.note || ''}}</td>
        </tr>`).join('');
      p("approvalTimeline", `<table>
        <thead><tr><th>When</th><th>Action</th><th>Node</th><th>Task</th><th>Actor</th><th>Note</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="6" class="muted">No approval events</td></tr>'}}</tbody>
      </table>`);
    }}
    function renderCompressionTimeline(data) {{
      const now = Date.now();
      const rows = (data.agent.compression_events || []).slice(0, 10).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, now)}}</td>
          <td>${{e.session || '-'}}</td>
          <td>${{e.at_turn || '-'}}</td>
          <td>${{e.reason || '-'}}</td>
        </tr>`).join('');
      p("compressionTimeline", `<table>
        <thead><tr><th>When</th><th>Session</th><th>Turn</th><th>Reason</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="4" class="muted">No compression events</td></tr>'}}</tbody>
      </table>`);
    }}
    function renderRecentObservability(data) {{
      const now = Date.now();
      const rows = (data.agent.recent_observability_events || []).slice(0, 15).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, now)}}</td>
          <td>${{e.kind || '-'}}</td>
          <td>${{e.title || '-'}}</td>
          <td>${{e.status || '-'}}</td>
          <td><code>${{e.trace_id || '-'}}</code></td>
          <td>${{e.detail || ''}}</td>
        </tr>`).join('');
      p("recentObservability", `<table>
        <thead><tr><th>When</th><th>Kind</th><th>Title</th><th>Status</th><th>Trace</th><th>Detail</th></tr></thead>
        <tbody>${{rows || '<tr><td colspan="6" class="muted">No recent observability events</td></tr>'}}</tbody>
      </table>`);
    }}
    function renderOps(data, report) {{
      const ops = report || data.ops || {{}};
      const notes = (ops.notes || []).slice(0, 6).map(note => `<li>${{note}}</li>`).join('');
      const attentionSections = (ops.attention_sections || []).slice(0, 4).map(section => {{
        const sectionRows = (section.rows || []).slice(0, 5).map(row => `
          <tr>
            <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
            <td>${{row.surface || '-'}}</td>
            <td><code>${{row.target || '-'}}</code></td>
            <td>${{row.action || '-'}}</td>
            <td>${{row.detail || '-'}}</td>
          </tr>`).join('');
        return `<div style="margin:8px 0;"><b>${{esc(section.title || section.key || 'Attention')}}</b> <span class="warn">(${{section.count || 0}})</span><table><thead><tr><th>When</th><th>Surface</th><th>Target</th><th>Action</th><th>Detail</th></tr></thead><tbody>${{sectionRows || '<tr><td colspan="5" class="muted">No rows</td></tr>'}}</tbody></table></div>`;
      }}).join('');
      const recentRows = (ops.recent_activity || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.surface || '-'}}</td>
          <td><code>${{row.target || '-'}}</code></td>
          <td>${{row.action || '-'}}</td>
          <td>${{row.pending_apply ? 'yes' : 'no'}}</td>
          <td>${{row.detail || '-'}}</td>
        </tr>`).join('');
      const pendingRows = (ops.pending_apply_rows || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.surface || '-'}}</td>
          <td><code>${{row.target || '-'}}</code></td>
          <td>${{row.action || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
          <td><button onclick="applyPendingOps('${{String(row.surface || '').replace(/'/g, '&#39;')}}', '${{String(row.target || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Apply this</button></td>
        </tr>`).join('');
      const status = ops.status === 'attention'
        ? '<span class="warn">attention</span>'
        : '<span class="ok">ok</span>';
      const surfaceFilter = esc(document.getElementById("opsSurfaceFilter")?.value || ops.surface || '');
      const actorFilter = esc(document.getElementById("opsActorFilter")?.value || ops.actor || '');
      const targetFilter = esc(document.getElementById("opsTargetFilter")?.value || ops.target || '');
      const pendingOnly = !!document.getElementById("opsPendingOnly")?.checked;
      const limit = esc(document.getElementById("opsLimit")?.value || String(ops.limit || 10));
      const offset = esc(document.getElementById("opsOffset")?.value || String(ops.offset || 0));
      const returned = ops.returned || 0;
      const total = ops.total || 0;
      p(
        "ops",
        `status: <b>${{status}}</b><br/>attention items: <b>${{ops.attention_items || 0}}</b><br/>agent profiles: <b>${{ops.agent_profile_total || 0}}</b><br/>agent recent actions: <b>${{ops.agent_recent_actions || 0}}</b><br/>agent execution total / delegated / failed: <b>${{ops.agent_execution_total || 0}}</b> / <b>${{ops.agent_routing_delegated || 0}}</b> / <b>${{ops.agent_execution_failed || 0}}</b><br/>approval waits / reflected / fallback / reload: <b>${{ops.agent_execution_approval_waits || 0}}</b> / <b>${{ops.agent_execution_reflected || 0}}</b> / <b>${{ops.agent_execution_fallback || 0}}</b> / <b>${{ops.agent_execution_reload || 0}}</b><br/>skill failed checks: <b>${{ops.skill_failed_checks || 0}}</b><br/>skill recent exports: <b>${{ops.skill_recent_exports || 0}}</b><br/>channel pending apply: <b>${{ops.channel_pending_reload || 0}}</b><br/>model routes: <b>${{ops.model_routes || 0}}</b><br/>pending apply total: <b>${{ops.pending_apply_total || 0}}</b><br/>returned / total: <b>${{returned}}</b> / <b>${{total}}</b><br/><ul>${{notes || '<li class="muted">No operator notes</li>'}}</ul>${{attentionSections || '<div class="muted">No grouped attention sections</div>'}}<div style="margin:10px 0 8px 0;"><b>Control Panel</b><br/><span class="muted">surface</span> <input id="opsSurfaceFilter" type="text" value="${{surfaceFilter}}" placeholder="agent/channel/skill/model_routing/agent_execution" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:240px;" /> <span class="muted">actor</span> <input id="opsActorFilter" type="text" value="${{actorFilter}}" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">target</span> <input id="opsTargetFilter" type="text" value="${{targetFilter}}" placeholder="finance_writer" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <label style="display:inline-flex; gap:4px; align-items:center;"><input id="opsPendingOnly" type="checkbox" ${{pendingOnly ? 'checked' : ''}} /> pending only</label> <span class="muted">limit</span> <input id="opsLimit" type="text" value="${{limit}}" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="opsOffset" type="text" value="${{offset}}" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadOpsSummary()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load ops</button></div><div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;"><button onclick="exportOpsSummary('json')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export ops JSON</button><button onclick="exportOpsSummary('csv')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export ops CSV</button><button onclick="previewPendingOps('all', '')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Preview apply</button><button onclick="applyPendingOps('all', '')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Mark Pending Applied</button><button onclick="executeReload(false)" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Execute Reload</button><button onclick="executeReload(true)" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Dry-run Preview</button><span id="opsSummaryStatus" class="muted"></span></div><div id="opsApplyPlanPanel" style="margin-top:6px;"></div><br/><b>Recent Activity</b><table><thead><tr><th>When</th><th>Surface</th><th>Target</th><th>Action</th><th>Pending</th><th>Detail</th></tr></thead><tbody>${{recentRows || '<tr><td colspan="6" class="muted">No recent control-plane activity</td></tr>'}}</tbody></table><br/><b>Pending Apply</b><table><thead><tr><th>When</th><th>Surface</th><th>Target</th><th>Action</th><th>Actor</th><th>Apply</th></tr></thead><tbody>${{pendingRows || '<tr><td colspan="6" class="muted">No pending apply actions</td></tr>'}}</tbody></table>`
      );
    }}
    function renderWorkflowWebhooks(data) {{
      const now = Date.now();
      const summary = data.agent.workflow_webhook_summary || {{}};
      const sourceCounts = (summary.source_counts || []).map(item =>
        `${{item.source || 'unknown'}}:${{item.count || 0}}`
      ).join(' / ');
      const rows = (data.agent.workflow_webhook_events || []).slice(0, 5).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, now)}}</td>
          <td>${{e.agent_id || '-'}}</td>
          <td>${{e.workflow_source || '-'}}</td>
          <td>${{e.workflow_run_id || '-'}}</td>
          <td>${{e.workflow_step || '-'}}</td>
          <td><code>${{e.trace_id || '-'}}</code></td>
        </tr>`).join('');
      p(
        "workflowWebhooks",
        `events: <b>${{summary.total || 0}}</b><br/>with trace / source: <b>${{summary.with_trace || 0}}</b> / <b>${{summary.with_source || 0}}</b><br/>sources: <b>${{sourceCounts || '-'}}</b><br/><br/><table><thead><tr><th>When</th><th>Agent</th><th>Source</th><th>Run</th><th>Step</th><th>Trace</th></tr></thead><tbody>${{rows || '<tr><td colspan="6" class="muted">No workflow webhook events</td></tr>'}}</tbody></table>`
      );
    }}
    function renderModelUsage(data) {{
      const summary = data.agent.model_routing_summary || {{}};
      const events = data.agent.model_routing_events || [];
      const modelFilter = (document.getElementById("modelUsageModelFilter")?.value || "").trim().toLowerCase();
      const reasonFilter = (document.getElementById("modelUsageReasonFilter")?.value || "").trim().toLowerCase();
      const limit = Number((document.getElementById("modelUsageLimit")?.value || "8").trim() || 8);
      const offset = Number((document.getElementById("modelUsageOffset")?.value || "0").trim() || 0);
      const sinceHours = Number((document.getElementById("modelUsageSinceHours")?.value || "0").trim() || 0);
      const fromAtMs = Number((document.getElementById("modelUsageFromAtMs")?.value || "0").trim() || 0);
      const toAtMs = Number((document.getElementById("modelUsageToAtMs")?.value || "0").trim() || 0);
      const now = Date.now();
      const modelRows = (summary.models || []).slice(0, 6).map(row => `
        <tr>
          <td>${{row.model || '-'}}</td>
          <td>${{row.count || 0}}</td>
        </tr>`).join('');
      const reasonRows = (summary.reasons || []).slice(0, 6).map(row => `
        <tr>
          <td>${{row.reason || '-'}}</td>
          <td>${{row.count || 0}}</td>
        </tr>`).join('');
      const intentRows = (summary.intents || []).slice(0, 6).map(row => `
        <tr>
          <td>${{row.intent || '-'}}</td>
          <td>${{row.count || 0}}</td>
        </tr>`).join('');
      const channelRows = (summary.channels || []).slice(0, 6).map(row => `
        <tr>
          <td>${{row.channel || '-'}}</td>
          <td>${{row.count || 0}}</td>
        </tr>`).join('');
      const bucketRows = (summary.buckets || []).slice(0, 8).map(row => `
        <tr>
          <td>${{fmtAgo(row.bucket_at_ms, now)}}</td>
          <td>${{row.count || 0}}</td>
        </tr>`).join('');
      const filteredEvents = events.filter(row => {{
        const okModel = !modelFilter || String(row.selected_model || '').toLowerCase() === modelFilter;
        const okReason = !reasonFilter || String(row.reason || '').toLowerCase() === reasonFilter;
        const okSince = !sinceHours || Number(row.at_ms || 0) >= (now - (sinceHours * 3600 * 1000));
        const okFrom = !fromAtMs || Number(row.at_ms || 0) >= fromAtMs;
        const okTo = !toAtMs || Number(row.at_ms || 0) <= toAtMs;
        return okModel && okReason && okSince && okFrom && okTo;
      }});
      const eventRows = filteredEvents.slice(Math.max(0, offset), Math.max(0, offset) + Math.max(1, limit)).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.selected_model || '-'}}</td>
          <td>${{row.reason || '-'}}</td>
          <td>${{row.intent_name || '-'}}</td>
          <td>${{row.channel || '-'}}</td>
          <td><code>${{row.trace_id || '-'}}</code></td>
        </tr>`).join('');
      p(
        "modelUsage",
        `routes: <b>${{summary.total || 0}}</b><br/>latest model: <b>${{summary.latest_model || '-'}}</b><br/>latest reason: <b>${{summary.latest_reason || '-'}}</b><br/>bucket size (minutes): <b>${{summary.bucket_minutes || 60}}</b><br/><br/><table><thead><tr><th>Model</th><th>Count</th></tr></thead><tbody>${{modelRows || '<tr><td colspan="2" class="muted">No model routes</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Reason</th><th>Count</th></tr></thead><tbody>${{reasonRows || '<tr><td colspan="2" class="muted">No routing reasons</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Intent</th><th>Count</th></tr></thead><tbody>${{intentRows || '<tr><td colspan="2" class="muted">No intents</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Channel</th><th>Count</th></tr></thead><tbody>${{channelRows || '<tr><td colspan="2" class="muted">No channels</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Bucket</th><th>Count</th></tr></thead><tbody>${{bucketRows || '<tr><td colspan="2" class="muted">No routing buckets</td></tr>'}}</tbody></table><br/><div style="margin:8px 0;">Filter model: <input id="modelUsageModelFilter" value="${{esc(modelFilter)}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> Filter reason: <input id="modelUsageReasonFilter" value="${{esc(reasonFilter)}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:180px;" /> Since h: <input id="modelUsageSinceHours" value="${{Number.isFinite(sinceHours) ? sinceHours : 0}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:70px;" /> From ms: <input id="modelUsageFromAtMs" value="${{Number.isFinite(fromAtMs) ? fromAtMs : 0}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:110px;" /> To ms: <input id="modelUsageToAtMs" value="${{Number.isFinite(toAtMs) ? toAtMs : 0}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:110px;" /> Limit: <input id="modelUsageLimit" value="${{Number.isFinite(limit) ? limit : 8}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:70px;" /> Offset: <input id="modelUsageOffset" value="${{Number.isFinite(offset) ? offset : 0}}" oninput="rerenderModelUsage()" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:70px;" /> <button onclick="exportModelUsage()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export JSON</button> <button onclick="exportModelUsageCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export CSV</button></div><table><thead><tr><th>When</th><th>Model</th><th>Reason</th><th>Intent</th><th>Channel</th><th>Trace</th></tr></thead><tbody>${{eventRows || '<tr><td colspan="6" class="muted">No model routing events</td></tr>'}}</tbody></table><br/><div style="margin:10px 0 6px 0;"><b>Historical Trend</b> <button onclick="loadModelTrend('hour')" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Hour</button> <button onclick="loadModelTrend('day')" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Day</button> <button onclick="loadModelTrend('week')" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Week</button> <span id="modelTrendStatus" class="muted"></span><div id="modelTrendPanel" style="margin-top:6px;"><span class="muted">Click Hour / Day / Week to load trend.</span></div></div>`
      );
    }}
    async function loadModelTrend(unit) {{
      const key = getKey();
      if (!key) return;
      const status = document.getElementById('modelTrendStatus');
      if (status) status.textContent = 'Loading...';
      try {{
        const resp = await fetch(`/api/v1/model-routing/summary?bucket_unit=${{unit}}&since_hours=168&max_groups=10`, {{
          headers: {{ 'X-API-Key': key }}
        }});
        const data = await resp.json();
        renderModelTrend(data, unit);
        if (status) status.textContent = `${{data.total || 0}} events, ${{(data.trend || []).length}} buckets`;
      }} catch(e) {{
        if (status) status.textContent = 'Error: ' + e.message;
      }}
    }}
    function renderModelTrend(data, unit) {{
      const panel = document.getElementById('modelTrendPanel');
      if (!panel) return;
      const trend = data.trend || [];
      if (!trend.length) {{
        panel.innerHTML = '<span class="muted">No trend data.</span>';
        return;
      }}
      const fmtBucket = (ms) => {{
        const d = new Date(ms);
        if (unit === 'week') return d.toISOString().slice(0, 10) + ' (week)';
        if (unit === 'day') return d.toISOString().slice(0, 10);
        return d.toISOString().slice(0, 16).replace('T', ' ');
      }};
      const rows = trend.map(b => {{
        const topModel = (b.models || [])[0];
        return `<tr><td>${{fmtBucket(b.bucket_at_ms)}}</td><td>${{b.count}}</td><td>${{topModel ? topModel.model : '-'}}</td><td>${{topModel ? topModel.count : '-'}}</td></tr>`;
      }}).join('');
      panel.innerHTML = `<table><thead><tr><th>Bucket (${{unit}})</th><th>Total</th><th>Top model</th><th>Count</th></tr></thead><tbody>${{rows}}</tbody></table>`;
    }}
    async function loadAgentModelPolicy(agentId) {{
      const key = getKey();
      const panel = document.getElementById('agentModelPolicyPanel');
      const statusEl = document.getElementById('agentModelPolicyStatus');
      if (!panel || !agentId) return;
      if (statusEl) statusEl.textContent = 'Loading...';
      try {{
        const resp = await fetch('/api/v1/agents/' + encodeURIComponent(agentId) + '/model-policy', {{ headers: {{ 'X-API-Key': key }} }});
        if (!resp.ok) {{ if (statusEl) statusEl.textContent = 'Error ' + resp.status; return; }}
        const d = await resp.json();
        if (statusEl) statusEl.textContent = '';
        const overridesHtml = Object.entries(d.task_type_model_overrides || {{}}).map(([k,v]) => `<tr><td>${{k}}</td><td>${{v}}</td></tr>`).join('') || '<tr><td colspan="2" class="muted">none</td></tr>';
        panel.innerHTML = `<b>Model Policy — ${{d.agent_id}}</b><table>
          <tr><td>model</td><td><b>${{d.model || '-'}}</b></td></tr>
          <tr><td>thinking_model</td><td>${{d.thinking_model || '-'}}</td></tr>
          <tr><td>fallback_model</td><td>${{d.fallback_model || '-'}}</td></tr>
          <tr><td>cost_model</td><td>${{d.cost_model || '-'}}</td></tr>
          <tr><td>stability_model</td><td>${{d.stability_model || '-'}}</td></tr>
        </table>task_type_model_overrides:<table><thead><tr><th>Task Type</th><th>Model</th></tr></thead><tbody>${{overridesHtml}}</tbody></table>`;
      }} catch(e) {{ if (statusEl) statusEl.textContent = 'Error: ' + e; }}
    }}
    async function executeReload(dryRun) {{
      const key = getKey();
      const status = document.getElementById('opsSummaryStatus');
      const resultPanel = document.getElementById('opsApplyPlanPanel');
      if (status) status.textContent = dryRun ? 'Previewing reload plan...' : 'Executing reload...';
      try {{
        const resp = await fetch('/api/v1/ops/reload-execute', {{
          method: 'POST',
          headers: {{ 'X-API-Key': key, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ dry_run: dryRun }})
        }});
        const d = await resp.json();
        if (status) status.textContent = dryRun ? `Plan: ${{(d.plan||[]).length}} entries` : `Executed: ${{d.executed}}, audit_only: ${{d.audit_only}}`;
        if (resultPanel) {{
          const rows = dryRun
            ? (d.plan||[]).map(e => `<tr><td>${{e.surface}}</td><td>${{e.execution_mode}}</td><td>${{e.target}}</td></tr>`).join('')
            : (d.results||[]).map(r => `<tr><td>${{r.surface}}</td><td>${{r.execution_mode}}</td><td>${{r.ok ? '<span class="ok">ok</span>' : '<span class="bad">fail</span>'}}</td><td>${{r.error||''}}</td></tr>`).join('');
          const header = dryRun ? '<tr><th>Surface</th><th>Mode</th><th>Target</th></tr>' : '<tr><th>Surface</th><th>Mode</th><th>Status</th><th>Error</th></tr>';
          resultPanel.innerHTML = `<table><thead>${{header}}</thead><tbody>${{rows || '<tr><td colspan="4" class="muted">empty plan</td></tr>'}}</tbody></table>`;
        }}
      }} catch(e) {{ if (status) status.textContent = 'Error: ' + e; }}
    }}
    function renderAgentProfiles(data) {{
      const agent = data.agent || {{}};
      const runtime = agent.runtime_state || {{}};
      const executionSummary = runtime.execution_summary || agent.execution_summary || {{}};
      const executionEvents = runtime.execution_events || agent.execution_events || [];
      const latestExecution = executionEvents[0] || null;
      const latestExecutionResult = latestExecution ? (latestExecution.execution_result || {{}}) : {{}};
      const latestExecutionTrace = latestExecutionResult.trace_summary || {{}};
      const rows = (agent.profiles || []).slice(0, 6).map(row => `
        <tr>
          <td>${{row.display_name || row.agent_id || '-'}}</td>
          <td><code>${{row.agent_id || '-'}}</code></td>
          <td>${{row.model || '-'}}</td>
          <td>${{row.planning_enabled ? 'yes' : 'no'}}</td>
          <td>${{(row.routing_keywords || []).slice(0, 2).join(', ') || '-'}}</td>
          <td>${{(row.skill_names || []).slice(0, 2).join(', ') || '-'}} | ${{row.allowed_tools_count || 0}} / ${{row.denied_tools_count || 0}}</td>
          <td><button onclick="inspectAgentProfile('${{row.agent_id}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Inspect</button> <button onclick="toggleAgentPlanning('${{row.agent_id}}', ${{!row.planning_enabled}})" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">${{row.planning_enabled ? 'Disable planning' : 'Enable planning'}}</button> <button onclick="updateAgentModel('${{row.agent_id}}', '${{String(row.model || '').replace(/'/g, '&#39;')}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Set model</button> <button onclick="updateAgentModelPolicy('${{row.agent_id}}', '${{encodeURIComponent(JSON.stringify({{ vision_model: row.vision_model || '', thinking_model: row.thinking_model || '', fallback_model: row.fallback_model || '', cost_model: row.cost_model || '', stability_model: row.stability_model || '', intent_model_overrides: row.intent_model_overrides || {{}}, task_type_model_overrides: row.task_type_model_overrides || {{}}, allowed_models: row.allowed_models || [] }}))}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Set model policy</button> ${{row.agent_id !== 'default' ? `<button onclick="updateAgentRoutingKeywords('${{row.agent_id}}', '${{encodeURIComponent(JSON.stringify(row.routing_keywords || []))}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Set routing</button> <button onclick="saveAgentProfile('${{row.agent_id}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Save profile</button> <button onclick="deleteAgentProfile('${{row.agent_id}}')" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Delete profile</button>` : ''}}</td>
        </tr>`).join('');
      const actionRows = (agent.recent_actions || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td><code>${{row.agent_id || '-'}}</code></td>
          <td>${{row.detail || row.action || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      const pendingRows = (agent.pending_reload_actions || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td><code>${{row.agent_id || '-'}}</code></td>
          <td>${{row.detail || row.action || '-'}}</td>
        </tr>`).join('');
      const executionRows = executionEvents.slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.intent_name || ((row.execution_intent || {{}}).contract_intent) || '-'}}</td>
          <td>${{((row.routing_decision || {{}}).action) || '-'}}</td>
          <td>${{((row.execution_intent || {{}}).path_type) || '-'}}</td>
          <td>${{((row.execution_result || {{}}).status) || '-'}}</td>
          <td>${{((row.execution_result || {{}}).failure_classification) || '-'}}</td>
          <td>${{describeExecutionTrace(((row.execution_result || {{}}).trace_summary) || {{}})}}</td>
        </tr>`).join('');
      p(
        "agents",
        `profiles: <b>${{agent.profiles_total || 0}}</b><br/>registered: <b>${{agent.registered_profiles || 0}}</b><br/>recent actions: <b>${{agent.actions_total || 0}}</b><br/>pending reload: <b>${{agent.pending_reload_count || 0}}</b> ${{agent.pending_reload ? '<span class="warn">(apply pending)</span>' : '<span class="ok">(clear)</span>'}}<br/>execution total / delegated / failed / gate3: <b>${{executionSummary.total || 0}}</b> / <b>${{executionSummary.delegated || 0}}</b> / <b>${{executionSummary.failed || 0}}</b> / <b>${{executionSummary.gate3_paths || 0}}</b><br/>approval waits / reflected / fallback / reload: <b>${{executionSummary.approval_waits || 0}}</b> / <b>${{executionSummary.reflected_runs || 0}}</b> / <b>${{executionSummary.fallback_runs || 0}}</b> / <b>${{executionSummary.reload_runs || 0}}</b><br/>latest execution: <b>${{latestExecution ? (latestExecutionResult.status || '-') : '-'}}</b> via <b>${{latestExecution ? ((latestExecution.execution_intent || {{}}).path_type || '-') : '-'}}</b> ${{latestExecutionTrace.fallback_used ? `<span class="muted">(fallback=${{esc(latestExecutionTrace.fallback_reason || 'used')}})</span>` : ''}}<br/>latest trace: reflections=<b>${{latestExecutionTrace.reflections_used || 0}}</b> / tool_failures=<b>${{latestExecutionTrace.tool_failures || 0}}</b> / reload=<b>${{latestExecutionTrace.reload_triggered ? 'yes' : 'no'}}</b><br/>last apply: <b>${{agent.last_apply ? fmtAgo(agent.last_apply.at_ms, Date.now()) : '-'}}</b><br/><button onclick="acknowledgeAgentReload()" style="margin-top:6px; padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Mark Applied</button> <button onclick="createAgentProfile()" style="margin-top:6px; padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Create profile</button><br/><br/><div style="margin:10px 0 8px 0;"><b>Route Preview</b><br/><span class="muted">channel</span> <input id="agentRoutePreviewChannel" type="text" placeholder="webchat" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">chat_id</span> <input id="agentRoutePreviewChatId" type="text" placeholder="chat-1" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">user_id</span> <input id="agentRoutePreviewUserId" type="text" placeholder="user-1" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">explicit agent</span> <input id="agentRoutePreviewExplicitAgentId" type="text" placeholder="optional" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">content</span> <input id="agentRoutePreviewContent" type="text" placeholder="need a bank campaign draft" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:260px;" /> <button onclick="previewAgentRoute()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Preview route</button> <button onclick="bindAgentRoute()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Bind route</button> <button onclick="clearAgentRoute()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Clear route</button> <span id="agentRoutePreviewStatus" class="muted"></span><div id="agentRoutePreviewResult" style="margin-top:6px;"></div></div><div style="margin:10px 0 8px 0;"><b>Agent History</b><br/><span class="muted">agent</span> <input id="agentHistoryAgentFilter" type="text" placeholder="finance_writer" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <span class="muted">action</span> <input id="agentHistoryActionFilter" type="text" placeholder="model_update" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <span class="muted">actor</span> <input id="agentHistoryActorFilter" type="text" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">limit</span> <input id="agentHistoryLimit" type="text" value="10" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="agentHistoryOffset" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadAgentActionHistory()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load history</button> <button onclick="exportAgentActionHistoryCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export agent CSV</button> <span id="agentHistoryStatus" class="muted"></span><div id="agentHistoryPanel" style="margin-top:6px;"></div></div><table><thead><tr><th>Name</th><th>ID</th><th>Model</th><th>Planning</th><th>Routing</th><th>Skills | Allow/Deny</th><th>Action</th></tr></thead><tbody>${{rows || '<tr><td colspan="7" class="muted">No agent profiles</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Intent</th><th>Routing</th><th>Path</th><th>Status</th><th>Failure</th><th>Trace</th></tr></thead><tbody>${{executionRows || '<tr><td colspan="7" class="muted">No recent execution events</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Agent</th><th>Pending Change</th></tr></thead><tbody>${{pendingRows || '<tr><td colspan="3" class="muted">No pending agent actions</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Agent</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{actionRows || '<tr><td colspan="4" class="muted">No agent actions</td></tr>'}}</tbody></table><br/><div style="margin:10px 0 8px 0;"><b>Active Sticky Routes</b> <button onclick="loadAgentRoutes()" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Refresh</button> <span id="agentRoutesStatus" class="muted"></span><div id="agentRoutesPanel" style="margin-top:6px;"><span class="muted">Click Refresh to load active routes.</span></div><div style="margin-top:6px;"><span class="muted">Clear by agent:</span> <input id="agentRoutesClearAgentId" type="text" placeholder="agent_id" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <button onclick="clearAgentRoutesByAgent(true)" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Preview</button> <button onclick="clearAgentRoutesByAgent(false)" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Clear</button></div></div><div style="margin:10px 0 8px 0;"><b>Model Policy</b> <span class="muted">agent_id:</span> <input id="agentModelPolicyId" type="text" placeholder="default" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <button onclick="loadAgentModelPolicy((document.getElementById('agentModelPolicyId')||{{}}).value||'default')" style="padding:3px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">View Policy</button> <span id="agentModelPolicyStatus" class="muted"></span><div id="agentModelPolicyPanel" style="margin-top:6px;"><span class="muted">Enter agent_id and click View Policy.</span></div></div>`
      );
    }}
    async function loadAgentRoutes() {{
      const key = getKey();
      if (!key) return;
      const status = document.getElementById('agentRoutesStatus');
      if (status) status.textContent = 'Loading...';
      try {{
        const resp = await fetch('/api/v1/agents/routes?limit=20', {{ headers: {{ 'X-API-Key': key }} }});
        const data = await resp.json();
        const panel = document.getElementById('agentRoutesPanel');
        if (!panel) return;
        if (!data.routes || data.routes.length === 0) {{
          panel.innerHTML = '<span class="muted">No active sticky routes.</span>';
          if (status) status.textContent = '';
          return;
        }}
        const rows = data.routes.map(r => `<tr><td><code>${{r.channel || '-'}}</code></td><td><code>${{r.chat_id || '-'}}</code></td><td><code>${{r.user_id || '-'}}</code></td><td><b>${{r.agent_id || '-'}}</b></td><td>${{r.updated_at_ms ? new Date(r.updated_at_ms).toLocaleString() : '-'}}</td></tr>`).join('');
        panel.innerHTML = `<span class="muted">Active routes: ${{data.total || data.routes.length}}</span><table><thead><tr><th>Channel</th><th>Chat ID</th><th>User ID</th><th>Agent</th><th>Updated</th></tr></thead><tbody>${{rows}}</tbody></table>`;
        if (status) status.textContent = '';
      }} catch(e) {{
        if (status) status.textContent = 'Error: ' + e.message;
      }}
    }}
    async function clearAgentRoutesByAgent(dryRun) {{
      const key = getKey();
      if (!key) return;
      const agentId = (document.getElementById('agentRoutesClearAgentId') || {{}}).value || '';
      if (!agentId) {{ alert('Enter an agent_id to clear routes for.'); return; }}
      if (!dryRun && !confirm('Clear all sticky routes for agent "' + agentId + '"?')) return;
      const status = document.getElementById('agentRoutesStatus');
      if (status) status.textContent = dryRun ? 'Previewing...' : 'Clearing...';
      try {{
        const resp = await fetch('/api/v1/agents/routes', {{
          method: 'DELETE',
          headers: {{ 'X-API-Key': key, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ agent_id: agentId, dry_run: dryRun }})
        }});
        const data = await resp.json();
        const panel = document.getElementById('agentRoutesPanel');
        if (dryRun) {{
          const preview = (data.preview_routes || []);
          panel.innerHTML = '<span class="muted">Preview: would clear ' + preview.length + ' route(s) for agent "' + agentId + '".</span>';
        }} else {{
          panel.innerHTML = '<span class="ok">Cleared ' + (data.cleared_count || 0) + ' route(s) for agent "' + agentId + '".</span>';
        }}
        if (status) status.textContent = '';
      }} catch(e) {{
        if (status) status.textContent = 'Error: ' + e.message;
      }}
    }}
    async function loadNotebooks() {{
      const key = localStorage.getItem('apiKey') || '';
      const panel = document.getElementById('notebooksManagementPanel');
      if (!panel) return;
      try {{
        const resp = await fetch('/api/v1/rag/notebooks', {{ headers: {{ 'X-API-Key': key }} }});
        if (!resp.ok) {{ panel.innerHTML = '<span class="bad">Load failed: ' + resp.status + '</span>'; return; }}
        const d = await resp.json();
        const rows = (d.notebooks || []).map(nb => `<tr>
          <td>${{nb.notebook_id || '-'}}</td>
          <td>${{nb.display_name || '-'}}</td>
          <td>${{nb.store_backend || 'default'}}</td>
          <td>${{nb.doc_count || 0}}</td>
          <td>${{nb.backend_ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'}}</td>
          <td>${{!nb.backend_ok ? `<button onclick="repairNotebook('${{String(nb.notebook_id||'').replace(/'/g,'&#39;')}}')" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;font-size:12px;">Repair</button>` : ''}}</td>
        </tr>`).join('');
        panel.innerHTML = `<b>Notebooks</b> (total: ${{d.total || 0}}) <button onclick="loadNotebooks()" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;font-size:12px;">Refresh</button><br/><div style="margin:6px 0;"><input id="newNotebookId" placeholder="notebook_id" style="padding:4px 6px;border:1px solid #d9e0ea;border-radius:6px;width:140px;" /><input id="newNotebookName" placeholder="display_name (opt)" style="padding:4px 6px;border:1px solid #d9e0ea;border-radius:6px;width:150px;margin-left:4px;" /><select id="newNotebookBackend" style="padding:4px 6px;border:1px solid #d9e0ea;border-radius:6px;margin-left:4px;"><option value="">default</option><option value="chroma">chroma</option><option value="memory">memory</option></select><button onclick="createNotebook()" style="padding:4px 8px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;margin-left:4px;">+ Create</button><span id="notebookCreateStatus" class="muted" style="margin-left:6px;"></span></div><table><thead><tr><th>ID</th><th>Name</th><th>Backend</th><th>Docs</th><th>Health</th><th>Action</th></tr></thead><tbody>${{rows || '<tr><td colspan="6" class="muted">No notebooks</td></tr>'}}</tbody></table>`;
      }} catch(e) {{ if (panel) panel.innerHTML = '<span class="bad">Error: ' + e + '</span>'; }}
    }}
    async function createNotebook() {{
      const key = localStorage.getItem('apiKey') || '';
      const id = (document.getElementById('newNotebookId') || {{}}).value || '';
      const name = (document.getElementById('newNotebookName') || {{}}).value || '';
      const backend = (document.getElementById('newNotebookBackend') || {{}}).value || '';
      const status = document.getElementById('notebookCreateStatus');
      if (!id) {{ if (status) status.textContent = 'notebook_id required'; return; }}
      try {{
        const resp = await fetch('/api/v1/rag/notebooks', {{
          method: 'POST',
          headers: {{ 'X-API-Key': key, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ notebook_id: id, display_name: name, store_backend: backend }})
        }});
        const d = await resp.json();
        if (resp.ok) {{ if (status) status.textContent = 'Created: ' + d.notebook_id; loadNotebooks(); }}
        else {{ if (status) status.textContent = 'Error ' + resp.status + ': ' + (d.detail || ''); }}
      }} catch(e) {{ if (status) status.textContent = 'Error: ' + e; }}
    }}
    async function repairNotebook(notebookId) {{
      const key = localStorage.getItem('apiKey') || '';
      if (!confirm('Attempt repair for notebook: ' + notebookId + '?')) return;
      try {{
        const resp = await fetch('/api/v1/rag/notebook/' + encodeURIComponent(notebookId) + '/repair', {{
          method: 'POST', headers: {{ 'X-API-Key': key }}
        }});
        const d = await resp.json();
        alert('Repair result: ok=' + d.ok + ' actions=' + (d.actions_taken || []).join(','));
        loadNotebooks();
      }} catch(e) {{ alert('Repair error: ' + e); }}
    }}
    function renderKnowledge(data) {{
      const summary = data.knowledge || {{}};
      const backendDiag = summary.backend_diagnostics || {{}};
      const rows = (summary.notebooks || []).slice(0, 5).map(nb => `
        <tr>
          <td>${{nb.name || '-'}}</td>
          <td>${{nb.id || '-'}}</td>
          <td>${{nb.doc_count || 0}}</td>
          <td>${{nb.store_backend || 'default'}}</td>
        </tr>`).join('');
      const backendRows = (backendDiag.detected_backends || []).slice(0, 5).map(row => `
        <tr>
          <td><code>${{row.backend || '-'}}</code></td>
          <td>${{row.healthy ? 'healthy' : 'attention'}}</td>
          <td>${{row.notebook_count || 0}}</td>
          <td>${{row.document_count || 0}}</td>
          <td>${{row.storage_present ? 'present' : '-'}}</td>
        </tr>`).join('');
      const cronRows = (summary.recent_cron_runs || []).slice(0, 5).map(run => `
        <tr>
          <td>${{run.job_name || '-'}}</td>
          <td>${{run.knowledge_notebook || '-'}}</td>
          <td>${{run.status || '-'}}</td>
          <td>${{run.chunks_added || 0}}</td>
        </tr>`).join('');
      const tenantRows = (summary.tenant_policies || []).slice(0, 5).map(row => `
        <tr>
          <td><code>${{row.tenant_id || '-'}}</code></td>
          <td>${{row.name || '-'}}</td>
          <td>${{row.enabled ? 'yes' : 'no'}}</td>
          <td>${{row.store_backend || 'default'}}</td>
        </tr>`).join('');
      const policyRows = (summary.recent_policy_changes || []).slice(0, 5).map(row => `
        <tr>
          <td>${{row.policy_kind || '-'}}</td>
          <td><code>${{row.tenant_id || '-'}}</code></td>
          <td><code>${{row.target_id || '-'}}</code></td>
          <td>${{row.before_store_backend || 'default'}} -> ${{row.after_store_backend || 'default'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      p(
        "knowledge",
        `notebooks: <b>${{summary.total_notebooks || 0}}</b><br/>documents: <b>${{summary.total_documents || 0}}</b><br/>non-empty: <b>${{summary.non_empty_notebooks || 0}}</b><br/>policy changes: <b>${{summary.policy_changes_total || 0}}</b><br/>chroma store: <b>${{summary.chroma_store_present ? 'present' : 'missing'}}</b><br/>backend health: <b>${{backendDiag.all_healthy ? 'healthy' : 'attention'}}</b><br/>configured backend: <b><code>${{backendDiag.configured_backend || 'chroma'}}</code></b><br/>knowledge cron runs ok/error: <b>${{summary.cron_runs_ok || 0}}</b> / <b>${{summary.cron_runs_error || 0}}</b><br/><br/><table><thead><tr><th>Name</th><th>ID</th><th>Docs</th><th>Backend</th></tr></thead><tbody>${{rows || '<tr><td colspan="4" class="muted">No notebooks</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Backend</th><th>Health</th><th>Notebooks</th><th>Docs</th><th>Storage</th></tr></thead><tbody>${{backendRows || '<tr><td colspan="5" class="muted">No backend diagnostics</td></tr>'}}</tbody></table><br/><div id="notebooksManagementPanel" style="margin:10px 0 8px 0; padding:8px; background:#f5f7fa; border-radius:6px;"><span class="muted">Notebooks: <button onclick="loadNotebooks()" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;font-size:12px;">Load</button></span></div><div style="margin:10px 0 8px 0;"><b>Notebook Backend Policy</b><br/><span class="muted">tenant_id</span> <input id="notebookPolicyTenantId" type="text" placeholder="default" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:160px;" /> <span class="muted">notebook_id</span> <input id="notebookPolicyNotebookId" type="text" placeholder="finance_kb" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:160px;" /> <span class="muted">backend</span> <select id="notebookPolicyBackend" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px;"><option value="">default</option><option value="chroma">chroma</option><option value="memory">memory</option></select> <span class="muted">retention docs</span> <input id="notebookPolicyRetentionMaxDocuments" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:80px;" /> <span class="muted">retention days</span> <input id="notebookPolicyRetentionMaxAgeDays" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:80px;" /> <button onclick="updateNotebookBackendPolicy()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Apply</button> <span id="notebookPolicyStatus" class="muted"></span></div><div style="margin:10px 0 8px 0;"><b>Tenant Backend Policy</b><br/><span class="muted">tenant_id</span> <input id="tenantPolicyTenantId" type="text" placeholder="default" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:180px;" /> <span class="muted">backend</span> <select id="tenantPolicyBackend" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px;"><option value="">default</option><option value="chroma">chroma</option><option value="memory">memory</option></select> <button onclick="updateTenantBackendPolicy()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Apply</button> <span id="tenantPolicyStatus" class="muted"></span></div><div style="margin:10px 0 8px 0;"><b>Knowledge Activity</b><br/><span class="muted">kind</span> <input id="knowledgeActivityKindFilter" type="text" placeholder="policy/cron" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:100px;" /> <span class="muted">tenant</span> <input id="knowledgeActivityTenantFilter" type="text" placeholder="default" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">actor</span> <input id="knowledgeActivityActorFilter" type="text" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">status</span> <input id="knowledgeActivityStatusFilter" type="text" placeholder="ok/error" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:90px;" /> <span class="muted">limit</span> <input id="knowledgeActivityLimit" type="text" value="10" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="knowledgeActivityOffset" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadKnowledgeActivityHistory()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load activity</button> <button onclick="exportKnowledgeActivityHistoryCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export activity CSV</button> <span id="knowledgeActivityHistoryStatus" class="muted"></span><div id="knowledgeActivityHistoryPanel" style="margin-top:6px;"></div></div><div style="margin:10px 0 8px 0;"><b>Policy History</b><br/><span class="muted">kind</span> <input id="knowledgePolicyKindFilter" type="text" placeholder="tenant" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:100px;" /> <span class="muted">actor</span> <input id="knowledgePolicyActorFilter" type="text" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">tenant</span> <input id="knowledgePolicyTenantFilter" type="text" placeholder="default" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">target</span> <input id="knowledgePolicyTargetFilter" type="text" placeholder="finance_kb" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">before</span> <input id="knowledgePolicyBeforeBackendFilter" type="text" placeholder="memory" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:90px;" /> <span class="muted">after</span> <input id="knowledgePolicyAfterBackendFilter" type="text" placeholder="chroma" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:90px;" /> <span class="muted">limit</span> <input id="knowledgePolicyLimit" type="text" value="10" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="knowledgePolicyOffset" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadKnowledgePolicyHistory()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load history</button> <button onclick="exportKnowledgePolicyHistoryCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export policy CSV</button> <span id="knowledgePolicyHistoryStatus" class="muted"></span><div id="knowledgePolicyHistoryPanel" style="margin-top:6px;"></div></div><table><thead><tr><th>Tenant</th><th>Name</th><th>Enabled</th><th>Backend</th></tr></thead><tbody>${{tenantRows || '<tr><td colspan="4" class="muted">No tenant policies</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Kind</th><th>Tenant</th><th>Target</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{policyRows || '<tr><td colspan="5" class="muted">No policy changes</td></tr>'}}</tbody></table><br/><table><thead><tr><th>Cron Job</th><th>Notebook</th><th>Status</th><th>Chunks</th></tr></thead><tbody>${{cronRows || '<tr><td colspan="4" class="muted">No knowledge cron runs</td></tr>'}}</tbody></table>`
      );
    }}
    function renderChannels(data) {{
      const summary = data.channels || {{}};
      const runtimeRows = (summary.runtime_controls || []).slice(0, 5).map(row => `
        <tr>
          <td><code>${{row.channel_name || '-'}}</code></td>
          <td>${{row.supported ? 'yes' : 'no'}}</td>
          <td>${{row.state || '-'}}</td>
          <td>${{row.running_instances || 0}}</td>
          <td><button onclick="controlChannelRuntime('${{String(row.channel_name || '').replace(/'/g, '&#39;')}}', 'start')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Start runtime</button> <button onclick="controlChannelRuntime('${{String(row.channel_name || '').replace(/'/g, '&#39;')}}', 'stop')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Stop runtime</button></td>
        </tr>`).join('');
      const pendingRows = (summary.pending_reload_actions || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.channel_name || '-'}}</td>
          <td>${{row.detail || row.action || '-'}}</td>
        </tr>`).join('');
      const actionRows = (summary.recent_actions || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.channel_name || '-'}}</td>
          <td>${{row.detail || row.action || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      const rows = (summary.rows || []).slice(0, 12).map(row => `
        <tr>
          <td>${{row.display_name || row.name || '-'}}</td>
          <td>${{row.enabled ? 'yes' : 'no'}}</td>
          <td>${{row.transport || '-'}}</td>
          <td>${{row.inbound_mode || '-'}}</td>
          <td>${{row.webhook_backed ? 'yes' : 'no'}}</td>
          <td>${{row.passive_capable ? 'yes' : 'no'}}</td>
          <td>${{row.rbac_enabled ? 'yes' : 'no'}}</td>
          <td><button onclick="toggleChannel('${{String(row.name || '').replace(/'/g, '&#39;')}}', ${{!row.enabled}})" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">${{row.enabled ? 'Disable' : 'Enable'}}</button></td>
        </tr>`).join('');
      p(
        "channels",
        `total/enabled: <b>${{summary.total || 0}}</b> / <b>${{summary.enabled || 0}}</b><br/>webhook-backed: <b>${{summary.webhook_backed || 0}}</b><br/>passive-capable: <b>${{summary.passive_capable || 0}}</b><br/>recent actions: <b>${{summary.actions_total || 0}}</b><br/>pending reload: <b>${{summary.pending_reload_count || 0}}</b> ${{summary.pending_reload ? '<span class="warn">(apply pending)</span>' : '<span class="ok">(clear)</span>'}}<br/>last apply: <b>${{summary.last_apply ? fmtAgo(summary.last_apply.at_ms, Date.now()) : '-'}}</b><br/><button onclick="acknowledgeChannelReload()" style="margin-top:6px; padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Mark Applied</button><br/><br/><div style="margin:10px 0 8px 0;"><b>Runtime Controls</b><br/><table><thead><tr><th>Channel</th><th>Supported</th><th>State</th><th>Running</th><th>Action</th></tr></thead><tbody>${{runtimeRows || '<tr><td colspan="5" class="muted">No in-process runtime controls</td></tr>'}}</tbody></table></div><div style="margin:10px 0 8px 0;"><b>Channel History</b><br/><span class="muted">channel</span> <input id="channelHistoryNameFilter" type="text" placeholder="slack" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">action</span> <input id="channelHistoryActionFilter" type="text" placeholder="disable" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">actor</span> <input id="channelHistoryActorFilter" type="text" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">limit</span> <input id="channelHistoryLimit" type="text" value="10" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="channelHistoryOffset" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadChannelActionHistory()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load history</button> <button onclick="exportChannelActionHistoryCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export channel CSV</button> <span id="channelHistoryStatus" class="muted"></span><div id="channelHistoryPanel" style="margin-top:6px;"></div></div><table><thead><tr><th>Name</th><th>Enabled</th><th>Transport</th><th>Inbound</th><th>Webhook</th><th>Passive</th><th>RBAC</th><th>Action</th></tr></thead><tbody>${{rows || '<tr><td colspan="8" class="muted">No channels</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Channel</th><th>Pending Change</th></tr></thead><tbody>${{pendingRows || '<tr><td colspan="3" class="muted">No pending reload actions</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Channel</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{actionRows || '<tr><td colspan="4" class="muted">No channel actions</td></tr>'}}</tbody></table>`
      );
    }}
    async function loadSkillsHealth() {{
      const key = localStorage.getItem('apiKey') || '';
      const bar = document.getElementById('skillsHealthBar');
      if (!bar) return;
      try {{
        const resp = await fetch('/api/v1/skills/health-summary', {{ headers: {{ 'X-API-Key': key }} }});
        if (!resp.ok) {{ bar.innerHTML = '<span class="bad">Health load failed: ' + resp.status + '</span>'; return; }}
        const d = await resp.json();
        bar.innerHTML = `Skills Health &nbsp;|&nbsp; Total: <b>${{d.total || 0}}</b> &nbsp; Enabled: <b>${{d.enabled || 0}}</b> (${{d.enabled_pct || 0}}%) &nbsp; Manifest OK: <b>${{d.manifest_ok || 0}}</b> &nbsp; Trusted: <b>${{d.trusted || 0}}</b> (${{d.trusted_pct || 0}}%) &nbsp; Runtime Covered: <b>${{d.runtime_covered || 0}}</b> &nbsp; Preflight OK: <b>${{d.preflight_ok || 0}}</b> (${{d.preflight_ok_pct || 0}}%) &nbsp; Issues: <b class="${{d.preflight_issues > 0 ? 'bad' : ''}}">${{d.preflight_issues || 0}}</b> &nbsp;<button onclick="loadSkillsHealth()" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Refresh</button>`;
      }} catch(e) {{ bar.innerHTML = '<span class="bad">Health error: ' + e + '</span>'; }}
    }}
    function toggleSkillSelectAll(checked) {{
      document.querySelectorAll('.skill-select-cb').forEach(cb => {{ cb.checked = checked; }});
    }}
    async function bulkSkillAction(action) {{
      const key = localStorage.getItem('apiKey') || '';
      const names = Array.from(document.querySelectorAll('.skill-select-cb:checked')).map(cb => cb.dataset.skillName).filter(Boolean);
      const status = document.getElementById('skillBulkActionStatus');
      if (!names.length) {{ if (status) status.textContent = 'No skills selected.'; return; }}
      if (status) status.textContent = 'Running...';
      try {{
        const resp = await fetch('/api/v1/skills/bulk-action', {{
          method: 'POST',
          headers: {{ 'X-API-Key': key, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ names, action, dry_run: false }})
        }});
        const d = await resp.json();
        if (status) status.textContent = `${{action}}: ${{d.succeeded || 0}} ok, ${{d.failed || 0}} failed`;
        if (typeof loadDashboard === 'function') loadDashboard();
        loadSkillsHealth();
      }} catch(e) {{ if (status) status.textContent = 'Error: ' + e; }}
    }}
    function renderSkills(data) {{
      const summary = data.skills || {{}};
      const trustSummary = Object.entries(summary.trust_breakdown || {{}})
        .map(([name, count]) => `${{name}}: <b>${{count}}</b>`)
        .join(' / ');
      const runtimeSummary = Object.entries(summary.runtime_mode_breakdown || {{}})
        .map(([name, count]) => `${{name}}: <b>${{count}}</b>`)
        .join(' / ');
      const actionRows = (summary.recent_actions || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.skill_name || '-'}}</td>
          <td>${{row.detail || row.action || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      const checkRows = (summary.recent_checks || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.skill_name || '-'}}</td>
          <td>${{row.ok ? 'ok' : 'issues'}}</td>
          <td>${{row.manifest_ok ? 'yes' : 'no'}} / ${{row.integrity_ok ? 'yes' : 'no'}} / ${{row.tests_present ? 'yes' : 'no'}}</td>
          <td>${{row.detail || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      const exportRows = (summary.recent_exports || []).slice(0, 5).map(row => `
        <tr>
          <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
          <td>${{row.skill_name || '-'}}</td>
          <td>${{row.out_path || '-'}}</td>
          <td>${{row.actor || '-'}}</td>
        </tr>`).join('');
      const rows = (summary.rows || []).slice(0, 6).map(skill => `
        <tr>
          <td><input type="checkbox" class="skill-select-cb" data-skill-name="${{String(skill.name || '').replace(/"/g, '&quot;')}}" /></td>
          <td>${{skill.name || '-'}}</td>
          <td>${{skill.enabled ? 'yes' : 'no'}}</td>
          <td>${{skill.manifest || '-'}}</td>
          <td>${{skill.enforce_ready ? 'yes' : 'no'}}</td>
          <td>${{skill.trust || '-'}}</td>
          <td>${{skill.runtime_intent_mode || skill.runtime_intent || '-'}}</td>
          <td>${{skill.tests_present ? 'yes' : 'no'}}</td>
          <td>${{skill.last_check ? (skill.last_check.ok ? 'ok' : 'issues') : '-'}}</td>
          <td><button onclick="inspectSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Inspect</button> <button onclick="toggleSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}', ${{!skill.enabled}})" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">${{skill.enabled ? 'Disable' : 'Enable'}}</button> <button onclick="preflightSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Check</button> <button onclick="exportSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export</button> <button onclick="previewSkillRestore('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Preview restore</button> <button onclick="restoreSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Restore skill</button> <button onclick="restoreSkillExport('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Restore export</button> <button onclick="setSkillPackagePolicy('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Set package policy</button> ${{skill.source === 'workspace' ? `<button onclick="uninstallSkill('${{String(skill.name || '').replace(/'/g, '&#39;')}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Uninstall</button>` : ''}}</td>
        </tr>`).join('');
      p(
        "skills",
        `total/enabled: <b>${{summary.total || 0}}</b> / <b>${{summary.enabled || 0}}</b><br/>available: <b>${{summary.available || 0}}</b><br/>enforce-ready: <b>${{summary.enforce_ready || 0}}</b><br/>tested/trusted/runtime intent: <b>${{summary.tested || 0}}</b> / <b>${{summary.trusted || 0}}</b> / <b>${{summary.runtime_intent || 0}}</b><br/>permissioned/scoped: <b>${{summary.permissioned || 0}}</b> / <b>${{summary.scoped || 0}}</b><br/>invalid/missing manifest: <b>${{summary.invalid || 0}}</b> / <b>${{summary.missing_manifest || 0}}</b><br/>packages versioned / journal pending: <b>${{summary.versioned_dirs || 0}}</b> / <b>${{summary.journal_pending || 0}}</b><br/>recent actions/checks/exports: <b>${{summary.actions_total || 0}}</b> / <b>${{summary.checks_total || 0}}</b> / <b>${{summary.exports_total || 0}}</b><br/>failed checks: <b>${{summary.failed_checks || 0}}</b><br/>trust breakdown: ${{trustSummary || '<span class="muted">none</span>'}}<br/>runtime modes: ${{runtimeSummary || '<span class="muted">none</span>'}}<br/><div style="margin:10px 0 8px 0;"><b>Install skill</b><br/><span class="muted">source path</span> <input id="skillInstallSource" type="text" placeholder="E:\\incoming\\skill" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:220px;" /> <span class="muted">name</span> <input id="skillInstallName" type="text" placeholder="optional override" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:140px;" /> <label><input id="skillInstallOverwrite" type="checkbox" /> overwrite</label> <label><input id="skillInstallStrictManifest" type="checkbox" /> strict manifest</label> <label><input id="skillInstallSanitize" type="checkbox" /> sanitize</label> <button onclick="installSkill()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Install</button></div><div style="margin:10px 0 8px 0;"><b>Batch State</b><br/><span class="muted">skills</span> <input id="skillBatchStateNames" type="text" placeholder="alpha,beta" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:220px;" /> <button onclick="setSkillBatchState(true)" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Enable selected</button> <button onclick="setSkillBatchState(false)" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Disable selected</button></div><div style="margin:10px 0 8px 0;"><b>Batch Checks</b><br/><span class="muted">skills</span> <input id="skillBatchPreflightNames" type="text" placeholder="alpha,beta (empty = all)" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:220px;" /> <label><input id="skillBatchPreflightIncludeDisabled" type="checkbox" checked /> include disabled</label> <button onclick="bulkPreflightSkills()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Check all skills</button></div><div style="margin:10px 0 8px 0;"><b>Package Cleanup</b><br/><span class="muted">retention hours</span> <input id="skillGcRetentionHours" type="text" value="24" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:90px;" /> <button onclick="previewSkillGc()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Preview cleanup</button> <button onclick="runSkillGc()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Run cleanup</button></div><div style="margin:10px 0 8px 0;"><b>Skill History</b><br/><span class="muted">skill</span> <input id="skillHistoryNameFilter" type="text" placeholder="alpha" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">kind</span> <input id="skillHistoryKindFilter" type="text" placeholder="action/check/export" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:150px;" /> <span class="muted">actor</span> <input id="skillHistoryActorFilter" type="text" placeholder="api_key" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /> <span class="muted">ok</span> <input id="skillHistoryOkFilter" type="text" placeholder="true/false" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:100px;" /> <span class="muted">limit</span> <input id="skillHistoryLimit" type="text" value="10" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <span class="muted">offset</span> <input id="skillHistoryOffset" type="text" value="0" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:60px;" /> <button onclick="loadSkillActivityHistory()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Load history</button> <button onclick="exportSkillActivityHistoryCsv()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Export skill CSV</button> <span id="skillHistoryStatus" class="muted"></span><div id="skillHistoryPanel" style="margin-top:6px;"></div></div><div id="skillsDetailPanel" style="margin:10px 0 8px 0;"></div><div id="skillsHealthBar" style="margin:6px 0 8px 0; padding:6px 8px; background:#f5f7fa; border-radius:6px; font-size:13px;"><span class="muted">Health: loading...</span> <button onclick="loadSkillsHealth()" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Refresh</button></div><div style="margin:4px 0 6px 0;"><button onclick="toggleSkillSelectAll(true)" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">☐ Select All</button> <button onclick="toggleSkillSelectAll(false)" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">☐ Deselect</button> <button onclick="bulkSkillAction('enable')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Enable Selected</button> <button onclick="bulkSkillAction('disable')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Disable Selected</button> <button onclick="bulkSkillAction('preflight')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; font-size:12px;">Check Selected</button> <span id="skillBulkActionStatus" class="muted"></span></div><table><thead><tr><th></th><th>Name</th><th>Enabled</th><th>Manifest</th><th>Ready</th><th>Trust</th><th>Intent</th><th>Tests</th><th>Last Check</th><th>Action</th></tr></thead><tbody>${{rows || '<tr><td colspan="10" class="muted">No skills</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Skill</th><th>Export Path</th><th>Actor</th></tr></thead><tbody>${{exportRows || '<tr><td colspan="4" class="muted">No skill exports</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Skill</th><th>Result</th><th>Manifest / Integrity / Tests</th><th>Detail</th><th>Actor</th></tr></thead><tbody>${{checkRows || '<tr><td colspan="6" class="muted">No skill checks</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Skill</th><th>Change</th><th>Actor</th></tr></thead><tbody>${{actionRows || '<tr><td colspan="4" class="muted">No skill actions</td></tr>'}}</tbody></table>`
      );
    }}
      function renderCrawler(data) {{
        const summary = data.crawler || {{}};
        const sourceRows = (summary.sources || []).slice(0, 8).map(row => {{
          const n = String(row.name || '').replace(/'/g, '&#39;');
          const isEnabled = row.enabled !== false;
          const toggleBtn = isEnabled
            ? `<button onclick="disableCrawlerSource('${{n}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Disable</button>`
            : `<button onclick="enableCrawlerSource('${{n}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Enable</button>`;
          return `<tr>
            <td>${{row.name || '-'}}</td>
            <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;">${{row.url || '-'}}</td>
            <td>${{row.notebook_id || '-'}}</td>
            <td>${{row.use_browser ? 'browser' : 'http'}}</td>
            <td>${{row.selector || '-'}}</td>
            <td>${{isEnabled ? '✓' : '⏸'}}</td>
            <td style="white-space:nowrap;">
              <button onclick="runCrawlerSource('${{n}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Run</button>
              ${{toggleBtn}}
              <button onclick="deleteCrawlerSource('${{n}}')" style="padding:2px 6px; border:1px solid #d9e0ea; border-radius:6px; background:#fff; color:#c0392b;">Del</button>
            </td>
          </tr>`;
        }}).join('');
        const runRows = (summary.recent_runs || []).slice(0, 5).map(row => `
          <tr>
            <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
            <td>${{row.source_name || '-'}}</td>
            <td>${{row.status || '-'}}</td>
            <td>${{row.change_status || '-'}}</td>
            <td>${{row.chunks_added || 0}}</td>
          </tr>`).join('');
        const sourceActionRows = (summary.recent_source_actions || []).slice(0, 5).map(row => `
          <tr>
            <td>${{fmtAgo(row.at_ms, Date.now())}}</td>
            <td>${{row.source_name || '-'}}</td>
            <td>${{row.notebook_id || '-'}}</td>
            <td>${{row.actor || '-'}}</td>
          </tr>`).join('');
        p(
          "crawler",
          `catalog: <code>${{summary.catalog_path || '-'}}</code><br/>sources/browser-backed: <b>${{summary.total_sources || 0}}</b> / <b>${{summary.browser_sources || 0}}</b><br/>runs/failed/source changes: <b>${{summary.runs_total || 0}}</b> / <b>${{summary.failed_runs || 0}}</b> / <b>${{summary.source_actions_total || 0}}</b><br/><div style="margin:8px 0; display:flex; gap:8px; flex-wrap:wrap;"><input id="crawlerSourceName" placeholder="name" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /><input id="crawlerSourceUrl" placeholder="url" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:220px;" /><input id="crawlerSourceNotebook" placeholder="notebook" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /><input id="crawlerSourceSelector" placeholder="selector" style="padding:4px 6px; border:1px solid #d9e0ea; border-radius:6px; width:120px;" /><label><input id="crawlerSourceBrowser" type="checkbox" /> browser</label><button onclick="saveCrawlerSource()" style="padding:4px 8px; border:1px solid #d9e0ea; border-radius:6px; background:#fff;">Save source</button></div><table><thead><tr><th>Name</th><th>URL</th><th>Notebook</th><th>Mode</th><th>Selector</th><th>Status</th><th>Actions</th></tr></thead><tbody>${{sourceRows || '<tr><td colspan="7" class="muted">No crawler sources</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Source</th><th>Status</th><th>Change</th><th>Chunks</th></tr></thead><tbody>${{runRows || '<tr><td colspan="5" class="muted">No crawler runs</td></tr>'}}</tbody></table><br/><table><thead><tr><th>When</th><th>Source</th><th>Notebook</th><th>Actor</th></tr></thead><tbody>${{sourceActionRows || '<tr><td colspan="4" class="muted">No crawler source changes</td></tr>'}}</tbody></table><br/><details style="margin-top:8px;"><summary style="cursor:pointer;font-weight:bold;">Schedules <button onclick="loadCrawlerSchedules()" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;font-size:12px;">Refresh</button> <button onclick="addCrawlerSchedulePrompt()" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;font-size:12px;">+ Add</button></summary><div id="crawlerSchedulesPanel" style="margin-top:6px;"><span class="muted">Click Refresh to load</span></div></details>`
        );
      }}
      function render(data) {{
        renderOps(data, window.__opsReport || null);
      const routerEvents = data.agent.intent_router_events || [];
      const latestRoute = routerEvents[0] || null;
      const compressionEvents = data.agent.compression_events || [];
      const latestCompression = compressionEvents[0] || null;
      p(
        "agent",
        `model: <b>${{data.agent.model}}</b><br/>planning: ${{badge(data.agent.planning_enabled)}}<br/>memory: ${{data.agent.memory_recall_mode}}<br/>compress: trigger=${{data.agent.compression_trigger_ratio}} / hysteresis=${{data.agent.compression_hysteresis_ratio}} / cooldown=${{data.agent.compression_cooldown_turns}}<br/>compress events: <b>${{compressionEvents.length}}</b><br/>latest compression: <b>${{latestCompression ? (latestCompression.reason || '-') : '-'}}</b><br/>latest model route: <b>${{(data.agent.model_routing_summary || {{}}).latest_model || '-'}}</b> (${{(data.agent.model_routing_summary || {{}}).latest_reason || '-'}})`
      );
      renderAgentProfiles(data);
      const routerSummary = data.agent.intent_router_summary || {{}};
      const routerRows = routerEvents.slice(0, 5).map(e => `
        <tr>
          <td>${{fmtAgo(e.at_ms, Date.now())}}</td>
          <td>${{e.intent_name || '-'}}</td>
          <td>${{e.route_status || '-'}}</td>
          <td><code>${{e.trace_id || '-'}}</code></td>
          <td>${{e.diagnostic || ''}}</td>
        </tr>`).join('');
      p(
        "intentRouter",
        `events: <b>${{routerSummary.total || 0}}</b><br/>direct success / failed / replan: <b>${{routerSummary.direct_success || 0}}</b> / <b>${{routerSummary.direct_failed || 0}}</b> / <b>${{routerSummary.replan || 0}}</b><br/>latest intent: <b>${{latestRoute ? (latestRoute.intent_name || '-') : '-'}}</b><br/>latest status: <b>${{latestRoute ? (latestRoute.route_status || '-') : '-'}}</b><br/>latest trace: <code>${{latestRoute ? (latestRoute.trace_id || '-') : '-'}}</code><br/><br/><table><thead><tr><th>When</th><th>Intent</th><th>Status</th><th>Trace</th><th>Diagnostic</th></tr></thead><tbody>${{routerRows || '<tr><td colspan="5" class="muted">No intent router events</td></tr>'}}</tbody></table>`
      );
      renderModelUsage(data);
      renderWorkflowWebhooks(data);
        renderChannels(data);
        renderSkills(data);
        renderCrawler(data);
        const cronRows = (data.cron.jobs || []).slice(0, 5).map(j => `
        <tr>
          <td>${{j.name || '-'}}</td>
          <td>${{j.schedule_kind || '-'}}</td>
          <td>${{j.target_kind || '-'}}</td>
          <td>${{j.knowledge_related ? 'yes' : 'no'}}</td>
          <td>${{j.last_status || '-'}}</td>
        </tr>`).join('');
      p("cron", `total: <b>${{data.cron.total_jobs}}</b><br/>enabled: <b>${{data.cron.enabled_jobs}}</b><br/>failed: <span class="${{data.cron.failed_jobs > 0 ? 'bad' : 'ok'}}">${{data.cron.failed_jobs}}</span><br/>webhook jobs: <b>${{data.cron.webhook_jobs || 0}}</b><br/>knowledge-related: <b>${{data.cron.knowledge_related_jobs || 0}}</b><br/><br/><table><thead><tr><th>Name</th><th>Schedule</th><th>Target</th><th>Knowledge</th><th>Last</th></tr></thead><tbody>${{cronRows || '<tr><td colspan="5" class="muted">No cron jobs</td></tr>'}}</tbody></table>`);
      renderKnowledge(data);
      p("security", `hardening: ${{badge(data.security.production_hardening)}}<br/>subagent guardrail: ${{badge(data.security.subagent_hard_guardrail)}}<br/>skill perms: <b>${{data.security.skill_permissions_mode}}</b>`);
      const running = (data.sidecars || []).filter(x => x.status === "running").length;
      p("sidecars", `running: <b>${{running}}</b> / ${{(data.sidecars || []).length}}<br/>details in raw snapshot`);
      const chainClass = data.node.approval_chain_ok ? 'ok' : 'bad';
      const chainText = data.node.approval_chain_ok ? 'ok' : (data.node.approval_chain_error || 'failed');
      p("node", `nodes: <b>${{data.node.active_nodes}}</b>/${{data.node.total_nodes}}<br/>queue pending/running/failed: <b>${{data.node.queue_pending}}</b> / <b>${{data.node.queue_running}}</b> / <b>${{data.node.queue_failed}}</b><br/>approvals pending/overdue: <b>${{data.node.pending_approval}}</b> / <span class="${{data.node.pending_approval_overdue > 0 ? 'bad' : 'ok'}}">${{data.node.pending_approval_overdue}}</span><br/>approval timeout rejected: <b>${{data.node.approval_timeout_rejected}}</b><br/>approval chain: <span class="${{chainClass}}">${{chainText}}</span> (checked=${{data.node.approval_chain_checked}})`);
      renderRecentObservability(data);
      renderNodeDetails(data);
      renderApprovalTimeline(data);
      renderCompressionTimeline(data);
      document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
    }}
    async function tick() {{
      try {{
        const storedKey = localStorage.getItem("zenClawSkillsApiKey") || "";
        const keyInput = document.getElementById("skillsApiKey");
        if (keyInput && !keyInput.value && storedKey) keyInput.value = storedKey;
        const r = await fetch('/api/status', {{ cache: 'no-store' }});
        if (!r.ok) throw new Error('http ' + r.status);
        const data = await r.json();
        render(data);
      }} catch (e) {{
        p("agent", '<span class="bad">fetch failed</span>');
      }}
      async function saveCrawlerSource() {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{
          if (status) status.textContent = "API key required for crawler source changes";
          return;
        }}
        const name = (document.getElementById("crawlerSourceName")?.value || "").trim();
        const url = (document.getElementById("crawlerSourceUrl")?.value || "").trim();
        const notebookId = (document.getElementById("crawlerSourceNotebook")?.value || "").trim() || "default";
        const selector = (document.getElementById("crawlerSourceSelector")?.value || "").trim();
        const useBrowser = !!document.getElementById("crawlerSourceBrowser")?.checked;
        if (!name || !url) {{
          if (status) status.textContent = "crawler source name and url are required";
          return;
        }}
        if (status) status.textContent = `saving crawler source ${{name}}...`;
        try {{
          const resp = await fetch("/api/v1/crawler/sources", {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              'X-API-Key': key
            }},
            body: JSON.stringify({{
              name,
              url,
              notebook_id: notebookId,
              selector,
              use_browser: useBrowser
            }})
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) {{
            throw new Error(body.detail || body.error || ('http ' + resp.status));
          }}
          if (status) status.textContent = `${{name}} saved`;
          await tick();
        }} catch (err) {{
          if (status) status.textContent = `crawler source save failed: ${{err.message || err}}`;
        }}
      }}
      async function disableCrawlerSource(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{ if (status) status.textContent = "API key required"; return; }}
        try {{
          const resp = await fetch(`/api/v1/crawler/sources/${{encodeURIComponent(name)}}/disable`, {{
            method: 'POST', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if (status) status.textContent = `${{name}} disabled`;
          await tick();
        }} catch (err) {{ if (status) status.textContent = `disable failed: ${{err.message || err}}`; }}
      }}
      async function enableCrawlerSource(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{ if (status) status.textContent = "API key required"; return; }}
        try {{
          const resp = await fetch(`/api/v1/crawler/sources/${{encodeURIComponent(name)}}/enable`, {{
            method: 'POST', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if (status) status.textContent = `${{name}} enabled`;
          await tick();
        }} catch (err) {{ if (status) status.textContent = `enable failed: ${{err.message || err}}`; }}
      }}
      async function deleteCrawlerSource(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{ if (status) status.textContent = "API key required"; return; }}
        if (!confirm(`Delete crawler source "${{name}}"? This cannot be undone.`)) return;
        try {{
          const resp = await fetch(`/api/v1/crawler/sources/${{encodeURIComponent(name)}}`, {{
            method: 'DELETE', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if (status) status.textContent = `${{name}} deleted`;
          await tick();
        }} catch (err) {{ if (status) status.textContent = `delete failed: ${{err.message || err}}`; }}
      }}
      async function runCrawlerSource(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById("skillsActionStatus");
        if (!key) {{
          if (status) status.textContent = "API key required for crawler run";
          return;
        }}
        if (status) status.textContent = `running crawler source ${{name}}...`;
        try {{
          const resp = await fetch("/api/v1/crawler/run", {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              'X-API-Key': key
            }},
            body: JSON.stringify({{ source_name: name }})
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) {{
            throw new Error(body.detail || body.error || ('http ' + resp.status));
          }}
          if (status) status.textContent = `${{name}} run -> ${{body.change_status || 'ok'}}`;
          await tick();
        }} catch (err) {{
          if (status) status.textContent = `crawler run failed: ${{err.message || err}}`;
        }}
      }}
      async function loadCrawlerSchedules() {{
        const key = getSkillsApiKey();
        const panel = document.getElementById('crawlerSchedulesPanel');
        if (!panel) return;
        try {{
          const resp = await fetch('/api/v1/crawler/schedules', {{ headers: {{ 'X-API-Key': key }} }});
          const d = await resp.json().catch(() => ({{}}));
          const rows = (d.schedules || []).map(s => {{
            const n = String(s.source_name || '').replace(/'/g, '&#39;');
            const enabledIcon = s.enabled !== false ? '✓' : '⏸';
            const toggleBtn = s.enabled !== false
              ? `<button onclick="pauseCrawlerSchedule('${{n}}')" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;">Pause</button>`
              : `<button onclick="resumeCrawlerSchedule('${{n}}')" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;">Resume</button>`;
            return `<tr><td>${{s.source_name||'-'}}</td><td><code>${{s.cron||'-'}}</code></td><td>${{s.tenant_id||'default'}}</td><td>${{enabledIcon}}</td><td style="white-space:nowrap;">${{toggleBtn}} <button onclick="deleteCrawlerSchedule('${{n}}')" style="padding:2px 6px;border:1px solid #d9e0ea;border-radius:6px;background:#fff;color:#c0392b;">Del</button></td></tr>`;
          }}).join('');
          panel.innerHTML = `<table><thead><tr><th>Source</th><th>Cron</th><th>Tenant</th><th>Status</th><th>Actions</th></tr></thead><tbody>${{rows||'<tr><td colspan="5" class="muted">No schedules</td></tr>'}}</tbody></table>`;
        }} catch(e) {{ if(panel) panel.innerHTML = '<span class="bad">Load failed: ' + e + '</span>'; }}
      }}
      async function addCrawlerSchedulePrompt() {{
        const key = getSkillsApiKey();
        const status = document.getElementById('skillsActionStatus');
        if (!key) {{ if(status) status.textContent = 'API key required'; return; }}
        const sourceName = prompt('Source name:');
        if (!sourceName) return;
        const cron = prompt('Cron expression (5 fields, e.g. "0 */6 * * *"):');
        if (!cron) return;
        try {{
          const resp = await fetch('/api/v1/crawler/schedules', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json', 'X-API-Key': key }},
            body: JSON.stringify({{ source_name: sourceName, cron }})
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if(status) status.textContent = `Schedule for ${{sourceName}} saved`;
          await loadCrawlerSchedules();
        }} catch(err) {{ if(status) status.textContent = `Add schedule failed: ${{err.message||err}}`; }}
      }}
      async function deleteCrawlerSchedule(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById('skillsActionStatus');
        if (!key) {{ if(status) status.textContent = 'API key required'; return; }}
        if (!confirm(`Delete schedule for "${{name}}"?`)) return;
        try {{
          const resp = await fetch(`/api/v1/crawler/schedules/${{encodeURIComponent(name)}}`, {{
            method: 'DELETE', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if(status) status.textContent = `Schedule for ${{name}} deleted`;
          await loadCrawlerSchedules();
        }} catch(err) {{ if(status) status.textContent = `Delete failed: ${{err.message||err}}`; }}
      }}
      async function pauseCrawlerSchedule(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById('skillsActionStatus');
        if (!key) {{ if(status) status.textContent = 'API key required'; return; }}
        try {{
          const resp = await fetch(`/api/v1/crawler/schedules/${{encodeURIComponent(name)}}/pause`, {{
            method: 'POST', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if(status) status.textContent = `Schedule for ${{name}} paused`;
          await loadCrawlerSchedules();
        }} catch(err) {{ if(status) status.textContent = `Pause failed: ${{err.message||err}}`; }}
      }}
      async function resumeCrawlerSchedule(name) {{
        const key = getSkillsApiKey();
        const status = document.getElementById('skillsActionStatus');
        if (!key) {{ if(status) status.textContent = 'API key required'; return; }}
        try {{
          const resp = await fetch(`/api/v1/crawler/schedules/${{encodeURIComponent(name)}}/resume`, {{
            method: 'POST', headers: {{ 'X-API-Key': key }}
          }});
          const body = await resp.json().catch(() => ({{}}));
          if (!resp.ok) throw new Error(body.detail || 'http ' + resp.status);
          if(status) status.textContent = `Schedule for ${{name}} resumed`;
          await loadCrawlerSchedules();
        }} catch(err) {{ if(status) status.textContent = `Resume failed: ${{err.message||err}}`; }}
      }}
    }}
    setInterval(tick, refreshMs);
    tick();
  </script>
</body>
</html>"""


def run_dashboard_server(
    config: "Config",
    *,
    host: str = "127.0.0.1",
    port: int = 18791,
    refresh_sec: int = 5,
) -> None:
    """Run blocking dashboard server."""
    from zen_claw.config.loader import get_data_dir

    data_dir = get_data_dir()
    _dashboard_token = str(os.environ.get("zen_claw_DASHBOARD_TOKEN", "")).strip()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/v1/health":
                body = json.dumps({"status": "ok", "service": "zen-claw"}, ensure_ascii=False).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                payload = json.dumps(build_dashboard_snapshot(config), ensure_ascii=False).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path in {"/", "/index.html"}:
                html = _render_html(
                    build_dashboard_snapshot(config), refresh_sec=refresh_sec
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/api/cron/run/"):
                if _dashboard_token:
                    provided_token = str(self.headers.get("X-zen-claw-Token") or "").strip()
                    if provided_token != _dashboard_token:
                        body = json.dumps(
                            {
                                "ok": False,
                                "error": "unauthorized: invalid or missing X-zen-claw-Token header",
                                "error_code": "dashboard_auth_failed",
                            }
                        ).encode("utf-8")
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                confirm = str(self.headers.get("X-zen-claw-Confirm") or "").strip().lower()
                if confirm != "run":
                    body = json.dumps(
                        {
                            "ok": False,
                            "error": "missing confirmation header: X-zen-claw-Confirm: run",
                        }
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                job_id = self.path.rsplit("/", 1)[-1].strip()
                if not job_id:
                    body = b'{"ok":false,"error":"job_id required"}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                result = trigger_cron_job_with_audit(job_id, data_dir=data_dir)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if result["ok"] else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), _Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
