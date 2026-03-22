import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from zen_claw.config.schema import Config
from zen_claw.dashboard.server import _render_html, build_dashboard_snapshot, run_dashboard_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, Any] | str]:
    req = urllib.request.Request(url, method=method, data=body)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")
            try:
                return int(resp.status), json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return int(resp.status), text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            return int(exc.code), json.loads(text)
        except (ValueError, json.JSONDecodeError):
            return int(exc.code), text


def _prepare_data_dir(data_dir: Path) -> None:
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "cron" / "jobs.json").write_text('{"jobs":[]}', encoding="utf-8")
    (data_dir / "channels" / "rate_limit_stats.json").write_text(
        '{"channels":{}}', encoding="utf-8"
    )
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )


def _start_server(cfg: Config, port: int) -> None:
    thread = threading.Thread(
        target=run_dashboard_server,
        kwargs={"config": cfg, "host": "127.0.0.1", "port": port, "refresh_sec": 1},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("dashboard server did not start")


def test_build_dashboard_snapshot_structure(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    snap = build_dashboard_snapshot(Config())
    assert "ops" in snap
    assert "agent" in snap
    assert "cron" in snap
    assert "knowledge" in snap
    assert "node" in snap
    assert "channels" in snap
    assert "sidecars" in snap


def test_dashboard_healthz(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(f"http://127.0.0.1:{port}/healthz")
    assert status == 200
    assert body == "ok"


def test_dashboard_status_endpoint(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(f"http://127.0.0.1:{port}/api/status")
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("cron", {}).get("total_jobs") == 0


def test_render_html_includes_observability_cards() -> None:
    html = _render_html(
        {
            "ops": {
                "status": "attention",
                "attention_items": 3,
                "agent_profile_total": 2,
                "agent_recent_actions": 1,
                "agent_pending_reload": 1,
                "skill_failed_checks": 1,
                "skill_recent_exports": 1,
                "channel_pending_reload": 1,
                "model_routes": 1,
                "notes": ["skills failed checks: 1", "agents pending apply: 1", "channels pending apply: 1"],
                "pending_apply_total": 2,
                "recent_activity": [
                    {
                        "at_ms": 124,
                        "surface": "agent",
                        "target": "finance_writer",
                        "action": "routing_keywords_update",
                        "pending_apply": True,
                    }
                ],
                "pending_apply_rows": [
                    {
                        "at_ms": 124,
                        "surface": "agent",
                        "target": "finance_writer",
                        "action": "routing_keywords_update",
                        "actor": "api_key",
                            "pending_apply": True,
                        }
                    ],
                    "attention_sections": [
                        {
                            "key": "pending_apply_audit_only",
                            "title": "Audit-only Pending Apply",
                            "count": 1,
                            "rows": [
                                {
                                    "at_ms": 124,
                                    "surface": "agent",
                                    "target": "finance_writer",
                                    "action": "routing_keywords_update",
                                    "detail": "mode=audit_only; capability=audit_only",
                                }
                            ],
                        }
                    ],
                },
            "agent": {
                "model": "m",
                "planning_enabled": True,
                "memory_recall_mode": "sqlite",
                "compression_trigger_ratio": 0.8,
                "compression_hysteresis_ratio": 0.5,
                "compression_cooldown_turns": 5,
                "compression_events": [],
                "model_routing_summary": {
                    "total": 1,
                    "latest_model": "weather-model",
                    "latest_reason": "intent_override:weather",
                    "models": [{"model": "weather-model", "count": 1}],
                    "reasons": [{"reason": "intent_override:weather", "count": 1}],
                    "intents": [{"intent": "weather", "count": 1}],
                    "channels": [{"channel": "cli", "count": 1}],
                },
                "model_routing_events": [
                    {
                        "at_ms": 111,
                        "trace_id": "trace-model-1",
                        "channel": "cli",
                        "intent_name": "weather",
                        "selected_model": "weather-model",
                        "reason": "intent_override:weather",
                    }
                ],
                "intent_router_events": [],
                "intent_router_summary": {},
                "workflow_webhook_events": [],
                "workflow_webhook_summary": {"source_counts": []},
                "recent_observability_events": [],
                "profiles": [
                    {
                        "agent_id": "default",
                        "display_name": "Default Agent",
                        "model": "m",
                        "planning_enabled": True,
                        "skill_names": [],
                        "skill_count": 0,
                        "routing_keywords": [],
                        "routing_keywords_count": 0,
                        "allowed_tools_count": 0,
                        "denied_tools_count": 0,
                    },
                    {
                        "agent_id": "finance_writer",
                        "display_name": "Finance Writer",
                        "model": "weather-model",
                        "planning_enabled": False,
                        "skill_names": ["content_gen"],
                        "skill_count": 1,
                        "routing_keywords": ["finance", "bank campaign"],
                        "routing_keywords_count": 2,
                        "allowed_tools_count": 2,
                        "denied_tools_count": 1,
                    }
                ],
                "profiles_total": 2,
                "registered_profiles": 2,
                "actions_total": 1,
                "pending_reload": True,
                "pending_reload_count": 1,
                "pending_reload_actions": [
                    {
                        "at_ms": 124,
                        "agent_id": "finance_writer",
                        "action": "routing_keywords_update",
                        "detail": "finance -> finance, bank campaign",
                    }
                ],
                "last_apply": {
                    "at_ms": 100,
                    "event": "agent.config.applied",
                    "actor": "api_key",
                },
                "recent_actions": [
                    {
                        "at_ms": 123,
                        "agent_id": "finance_writer",
                        "action": "routing_keywords_update",
                        "actor": "api_key",
                        "detail": "finance -> finance, bank campaign",
                    }
                ],
            },
            "cron": {"total_jobs": 0, "enabled_jobs": 0, "failed_jobs": 0},
            "knowledge": {
                "total_notebooks": 0,
                "total_documents": 0,
                "non_empty_notebooks": 0,
                "chroma_store_present": False,
                "notebooks": [],
                "tenant_policies": [],
            },
            "skills": {
                "total": 1,
                "enabled": 1,
                "available": 1,
                "enforce_ready": 1,
                "tested": 1,
                "trusted": 1,
                "runtime_intent": 1,
                "permissioned": 1,
                "scoped": 1,
                "invalid": 0,
                "missing_manifest": 0,
                "actions_total": 1,
                "checks_total": 1,
                "exports_total": 1,
                "failed_checks": 0,
                "trust_breakdown": {"trusted": 1},
                "runtime_mode_breakdown": {"skill_first": 1},
                "recent_exports": [
                    {
                        "at_ms": 125,
                        "skill_name": "alpha",
                        "out_path": "E:/nano-claw-public/.zen-claw/exports/alpha.zip",
                        "actor": "api_key",
                    }
                ],
                "recent_checks": [
                    {
                        "at_ms": 124,
                        "skill_name": "alpha",
                        "ok": True,
                        "manifest_ok": True,
                        "integrity_ok": True,
                        "tests_present": True,
                        "detail": "manifest/integrity checks passed",
                        "actor": "api_key",
                    }
                ],
                "recent_actions": [
                    {
                        "at_ms": 123,
                        "skill_name": "alpha",
                        "action": "enable",
                        "actor": "api_key",
                    }
                ],
                "rows": [
                    {
                        "name": "alpha",
                        "enabled": True,
                        "manifest": "valid",
                        "enforce_ready": True,
                        "trust": "trusted",
                        "runtime_intent_mode": "skill_first",
                        "tests_present": True,
                        "last_check": {
                            "ok": True,
                            "detail": "manifest/integrity checks passed",
                        },
                    }
                ],
            },
            "channels": {
                "rows": [
                    {
                        "name": "slack",
                        "display_name": "Slack",
                        "enabled": True,
                        "rbac_enabled": False,
                        "admins": 0,
                        "users": 0,
                        "transport": "socket_or_http",
                        "inbound_mode": "socket_mode_or_webhook",
                        "webhook_backed": True,
                        "passive_capable": True,
                    }
                ],
                "total": 1,
                "enabled": 1,
                "webhook_backed": 1,
                "passive_capable": 1,
                "runtime_controls": [
                    {
                        "channel_name": "webchat",
                        "supported": True,
                        "state": "running",
                        "running_instances": 1,
                    }
                ],
                "actions_total": 1,
                "pending_reload": True,
                "pending_reload_count": 1,
                "pending_reload_actions": [
                    {
                        "at_ms": 122,
                        "channel_name": "slack",
                        "action": "enable",
                    }
                ],
                "last_apply": {
                    "at_ms": 100,
                    "event": "channels.config.applied",
                    "actor": "api_key",
                },
                "recent_actions": [
                    {
                        "at_ms": 123,
                        "channel_name": "slack",
                        "action": "enable",
                        "actor": "api_key",
                    }
                ],
            },
            "security": {
                "production_hardening": False,
                "subagent_hard_guardrail": True,
                "skill_permissions_mode": "off",
            },
            "sidecars": [],
            "node": {
                "active_nodes": 0,
                "total_nodes": 0,
                "queue_pending": 0,
                "queue_running": 0,
                "queue_failed": 0,
                "pending_approval": 0,
                "pending_approval_overdue": 0,
                "approval_timeout_rejected": 0,
                "approval_chain_ok": True,
                "approval_chain_checked": 0,
                "approval_chain_error": "",
                "nodes": [],
                "approval_timeline": [],
            },
        },
        refresh_sec=1,
    )

    assert "Intent Router" in html
    assert "Agents" in html
    assert "Operations Summary" in html
    assert "Recent Activity" in html
    assert "Pending Apply" in html
    assert "Audit-only Pending Apply" in html
    assert "pending apply total" in html
    assert "Control Panel" in html
    assert "No grouped attention sections" in html or "Failed Checks" in html or "Runtime Unsupported" in html
    assert "Load ops" in html
    assert "pending only" in html
    assert "Preview apply" in html
    assert "Mark Pending Applied" in html
    assert "Apply this" in html
    assert "opsApplyPlanPanel" in html
    assert "<th>Surface</th><th>Targets</th><th>Exec</th><th>Audit-only</th><th>Unsupported</th><th>Target List</th>" in html
    assert "<th>Step</th><th>Surface</th><th>Target</th><th>Action</th><th>Mode</th><th>Capability</th>" in html
    assert "<th>Step</th><th>Surface</th><th>Target</th><th>Action</th><th>Mode</th><th>Capability</th><th>Status</th><th>Result</th>" in html
    assert "opsSurfaceFilter" in html
    assert "opsActorFilter" in html
    assert "opsTargetFilter" in html
    assert "opsPendingOnly" in html
    assert "opsLimit" in html
    assert "opsOffset" in html
    assert "returned / total" in html
    assert "attention items" in html
    assert "skills failed checks: 1" in html
    assert "Export ops JSON" in html
    assert "Export ops CSV" in html
    assert "Model Usage" in html
    assert "weather-model" in html
    assert "Export JSON" in html
    assert "Export CSV" in html
    assert "Filter model" in html
    assert "bucket size (minutes)" in html
    assert "Since h:" in html
    assert "From ms:" in html
    assert "To ms:" in html
    assert "Limit:" in html
    assert "Offset:" in html
    assert "<th>Intent</th><th>Count</th>" in html
    assert "<th>Channel</th><th>Count</th>" in html
    assert "<th>Bucket</th><th>Count</th>" in html
    assert "Workflow Webhooks" in html
    assert "Channels" in html
    assert "Crawler" in html
    assert "socket_or_http" in html
    assert "Disable planning" in html
    assert "Set model" in html
    assert "Set model policy" in html
    assert "Set routing" in html
    assert "Create profile" in html
    assert "Save profile" in html
    assert "Delete profile" in html
    assert "Inspect" in html
    assert "content_gen" in html
    assert "Skills | Allow/Deny" in html
    assert "allowed_tools_count" in html
    assert "denied_tools_count" in html
    assert "Route Preview" in html
    assert "Preview route" in html
    assert "Bind route" in html
    assert "Clear route" in html
    assert "Agent History" in html
    assert "Load history" in html
    assert "Export agent CSV" in html
    assert "Mark Applied" in html
    assert "pending reload" in html
    assert "<th>When</th><th>Agent</th><th>Pending Change</th>" in html
    assert "<th>When</th><th>Agent</th><th>Change</th><th>Actor</th>" in html
    assert "finance, bank campaign" in html
    assert "<th>When</th><th>Channel</th><th>Change</th><th>Actor</th>" in html
    assert "pending reload" in html
    assert "Mark Applied" in html
    assert "Channel History" in html
    assert "Export channel CSV" in html
    assert "Runtime Controls" in html
    assert "Start runtime" in html
    assert "Stop runtime" in html
    assert "Supported" in html
    assert "Knowledge" in html
    assert "backend health" in html
    assert "No backend diagnostics" in html
    assert "Knowledge Activity" in html
    assert "Export activity CSV" in html
    assert "Management API key" in html
    assert "Disable" in html
    assert "Check" in html
    assert "Export" in html
    assert "Install skill" in html
    assert "Batch State" in html
    assert "Enable selected" in html
    assert "Disable selected" in html
    assert "Batch Checks" in html
    assert "Check all skills" in html
    assert "strict manifest" in html
    assert "Skill History" in html
    assert "Export skill CSV" in html
    assert "Restore export" in html
    assert "Preview restore" in html
    assert "Restore skill" in html
    assert "export dir/version" in html
    assert "Set package policy" in html
    assert "Uninstall" in html
    assert "knowledgePolicyTargetFilter" in html
    assert "knowledgePolicyBeforeBackendFilter" in html
    assert "knowledgePolicyAfterBackendFilter" in html
    assert "skillsDetailPanel" in html
    assert "packages versioned / journal pending" in html
    assert "Package Cleanup" in html
    assert "Preview cleanup" in html
    assert "Run cleanup" in html
    assert "Save source" in html
    assert "No crawler sources" in html
    assert "tested/trusted/runtime intent" in html
    assert "recent actions" in html
    assert "recent actions/checks/exports" in html
    assert "failed checks" in html
    assert "<th>When</th><th>Skill</th><th>Export Path</th><th>Actor</th>" in html
    assert "<th>When</th><th>Skill</th><th>Result</th><th>Manifest / Integrity / Tests</th><th>Detail</th><th>Actor</th>" in html
    assert "<th>Name</th><th>Enabled</th><th>Manifest</th><th>Ready</th><th>Trust</th><th>Intent</th><th>Tests</th><th>Last Check</th><th>Action</th>" in html
    assert "<th>When</th><th>Skill</th><th>Change</th><th>Actor</th>" in html
    assert "trusted" in html
    assert "Tenant Backend Policy" in html
    assert "Notebook Backend Policy" in html
    assert "retention docs" in html
    assert "retention days" in html
    assert "Policy History" in html
    assert "Load history" in html
    assert "Export policy CSV" in html
    assert "No policy changes" in html
    assert "Compression Timeline" in html
    assert "Recent Observability" in html


def test_post_cron_without_token_env_is_backward_compatible(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run"},
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("ok") is True


def test_post_cron_with_token_missing_header_returns_401(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "secret-1")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run"},
    )
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("error_code") == "dashboard_auth_failed"


def test_post_cron_with_valid_token_returns_non_401(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _prepare_data_dir(data_dir)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.dashboard.server.trigger_cron_job_with_audit",
        lambda job_id, *, data_dir: {"ok": True, "job_id": job_id},
    )
    monkeypatch.setenv("zen_claw_DASHBOARD_TOKEN", "secret-1")
    port = _free_port()
    _start_server(Config(), port)
    status, body = _request(
        f"http://127.0.0.1:{port}/api/cron/run/job-a",
        method="POST",
        headers={"X-zen-claw-Confirm": "run", "X-zen-claw-Token": "secret-1"},
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("ok") is True


def teardown_module() -> None:
    os.environ.pop("zen_claw_DASHBOARD_TOKEN", None)
