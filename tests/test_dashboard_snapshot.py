import json
from pathlib import Path

from zen_claw.config.schema import Config
from zen_claw.dashboard.server import build_dashboard_snapshot


def test_build_dashboard_snapshot_includes_cron_sidecar_rate_limit(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    cfg.agents.defaults.model = "openrouter/anthropic/claude-3.5-sonnet"
    cfg.tools.policy.production_hardening = True
    cfg.channels.telegram.admins = ["a1"]
    cfg.channels.telegram.users = ["u1", "u2"]

    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)

    (data_dir / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "name": "ok-job",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 1000},
                        "state": {"lastStatus": "ok", "nextRunAtMs": 100},
                    },
                    {
                        "id": "j2",
                        "name": "err-job",
                        "enabled": False,
                        "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
                        "state": {"lastStatus": "error", "lastError": "boom"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "channels" / "rate_limit_stats.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {"delayed_count": 2, "dropped_count": 1, "last_delay_ms": 80},
                }
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "nodes" / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": {
                    "n1": {
                        "name": "phone-a",
                        "platform": "android",
                        "status": "active",
                        "last_seen_ms": 123,
                    },
                    "n2": {
                        "name": "phone-b",
                        "platform": "ios",
                        "status": "inactive",
                        "last_seen_ms": 88,
                    },
                },
                "tasks": [
                    {
                        "task_id": "t1",
                        "node_id": "n1",
                        "task_type": "agent.prompt",
                        "status": "pending",
                    },
                    {
                        "task_id": "t2",
                        "node_id": "n1",
                        "task_type": "agent.prompt",
                        "status": "running",
                    },
                    {
                        "task_id": "t3",
                        "node_id": "n2",
                        "task_type": "agent.prompt",
                        "status": "pending_approval",
                        "approval": {"expires_at_ms": 1},
                    },
                    {
                        "task_id": "t4",
                        "node_id": "n2",
                        "task_type": "agent.prompt",
                        "status": "rejected",
                        "error": "approval timeout",
                    },
                ],
                "approval_events": [
                    {
                        "event_id": "e1",
                        "task_id": "t3",
                        "node_id": "n2",
                        "action": "submitted",
                        "actor": "system",
                        "note": "task requires approval",
                        "at_ms": 100,
                        "prev_hash": "",
                        "hash": "bad-hash",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status",
        lambda _cfg: [{"name": "sec-execd", "status": "running", "health": True}],
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["agent"]["model"] == "openrouter/anthropic/claude-3.5-sonnet"
    assert snapshot["security"]["production_hardening"] is True
    assert snapshot["cron"]["total_jobs"] == 2
    assert snapshot["cron"]["enabled_jobs"] == 1
    assert snapshot["cron"]["failed_jobs"] == 1
    assert snapshot["cron"]["webhook_jobs"] == 0
    assert snapshot["cron"]["knowledge_related_jobs"] == 0
    assert snapshot["knowledge"]["total_notebooks"] == 0
    assert snapshot["knowledge"]["total_documents"] == 0
    assert snapshot["sidecars"][0]["name"] == "sec-execd"
    assert snapshot["rate_limit"]["runtime"][0]["channel"] == "telegram"
    assert snapshot["node"]["total_nodes"] == 2
    assert snapshot["node"]["active_nodes"] == 1
    assert snapshot["node"]["queue_pending"] == 2
    assert snapshot["node"]["queue_running"] == 1
    assert snapshot["node"]["queue_failed"] == 1
    assert snapshot["node"]["pending_approval"] == 1
    assert snapshot["node"]["pending_approval_overdue"] == 1
    assert snapshot["node"]["approval_timeout_rejected"] == 1
    assert snapshot["node"]["nodes"][0]["name"] == "phone-a"
    assert "allow_gateway_tasks" in snapshot["node"]["nodes"][0]
    assert "latest_task_status" in snapshot["node"]["nodes"][0]
    assert len(snapshot["node"]["approval_timeline"]) == 1
    assert snapshot["node"]["approval_timeline"][0]["action"] == "submitted"
    assert snapshot["node"]["approval_chain_ok"] is False

    rows = {row["name"]: row for row in snapshot["channels"]}
    assert rows["telegram"]["rbac_enabled"] is True
    assert rows["telegram"]["admins"] == 1
    assert rows["telegram"]["users"] == 2
    assert "api_base" not in snapshot["providers"][0]


def test_build_dashboard_snapshot_detects_approval_chain_tamper(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)

    event = {
        "event_id": "e1",
        "task_id": "t1",
        "node_id": "n1",
        "action": "submitted",
        "actor": "ops",
        "note": "ok",
        "at_ms": 100,
        "prev_hash": "",
        "hash": "deadbeef",
    }
    (data_dir / "nodes" / "state.json").write_text(
        json.dumps({"version": 1, "nodes": {}, "tasks": [], "approval_events": [event]}),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["node"]["approval_chain_ok"] is False
    assert snapshot["node"]["approval_chain_checked"] == 0
    assert snapshot["node"]["approval_chain_error"] in {
        "approval hash mismatch",
        "approval chain broken",
    }


def test_build_dashboard_snapshot_node_details_include_latest_task(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": {
                    "n1": {
                        "name": "node-a",
                        "platform": "android",
                        "status": "active",
                        "last_seen_ms": 50,
                        "policy": {
                            "allow_gateway_tasks": False,
                            "max_running_tasks": 2,
                            "approval_required_count": 2,
                        },
                    },
                },
                "tasks": [
                    {
                        "task_id": "t1",
                        "node_id": "n1",
                        "task_type": "message.send",
                        "status": "pending",
                        "updated_at_ms": 10,
                    },
                    {
                        "task_id": "t2",
                        "node_id": "n1",
                        "task_type": "agent.prompt",
                        "status": "running",
                        "updated_at_ms": 20,
                    },
                ],
                "approval_events": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    snapshot = build_dashboard_snapshot(cfg)
    row = snapshot["node"]["nodes"][0]
    assert row["allow_gateway_tasks"] is False
    assert row["max_running_tasks"] == 2
    assert row["approval_required_count"] == 2
    assert row["latest_task_type"] == "agent.prompt"
    assert row["latest_task_status"] == "running"


def test_build_dashboard_snapshot_includes_recent_observability_events(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": {},
                "tasks": [],
                "approval_events": [
                    {
                        "event_id": "e1",
                        "task_id": "t1",
                        "node_id": "n1",
                        "action": "approved",
                        "actor": "ops",
                        "note": "ok",
                        "at_ms": 100,
                        "prev_hash": "",
                        "hash": "deadbeef",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "intent_router.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 200,
                "intent_name": "weather",
                "route_status": "direct_failed",
                "trace_id": "trace-1",
                "diagnostic": "days_limit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    monkeypatch.setattr(
        "zen_claw.dashboard.server.Path.home", lambda: tmp_path
    )
    sessions_dir = tmp_path / ".zen-claw" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "s1.jsonl").write_text(
        json.dumps(
            {
                "metadata": {
                    "compression_events": [
                        {"at_ms": 150, "at_turn": 3, "reason": "token_ratio"}
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = build_dashboard_snapshot(cfg)
    events = snapshot["agent"]["recent_observability_events"]
    assert len(events) == 3
    assert [event["kind"] for event in events] == [
        "intent_router",
        "compression",
        "approval",
    ]


def test_build_dashboard_snapshot_includes_workflow_webhook_events(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "workflow_webhook.log.jsonl").write_text(
        (
            json.dumps(
                {
                    "at_ms": 200,
                    "agent_id": "agent-flow",
                    "trace_id": "trace-2",
                    "workflow_source": "n8n",
                    "workflow_run_id": "run-2",
                    "workflow_step": "step-a",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "at_ms": 190,
                    "agent_id": "agent-flow-2",
                    "trace_id": "trace-3",
                    "workflow_source": "coze",
                    "workflow_run_id": "run-3",
                    "workflow_step": "step-b",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "at_ms": 180,
                    "agent_id": "agent-flow-3",
                    "trace_id": "",
                    "workflow_source": "n8n",
                    "workflow_run_id": "run-4",
                    "workflow_step": "step-c",
                }
            )
            + "\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    monkeypatch.setattr("zen_claw.dashboard.server.Path.home", lambda: tmp_path)

    snapshot = build_dashboard_snapshot(cfg)

    assert snapshot["agent"]["workflow_webhook_summary"]["total"] == 3
    assert snapshot["agent"]["workflow_webhook_summary"]["with_trace"] == 2
    assert snapshot["agent"]["workflow_webhook_summary"]["with_source"] == 3
    assert snapshot["agent"]["workflow_webhook_summary"]["source_counts"] == [
        {"source": "n8n", "count": 2},
        {"source": "coze", "count": 1},
    ]
    assert snapshot["agent"]["workflow_webhook_events"][0]["workflow_source"] == "n8n"
    assert snapshot["agent"]["recent_observability_events"][0]["kind"] == "workflow_webhook"


def test_build_dashboard_snapshot_includes_model_routing_events(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "model_routing.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 250,
                "trace_id": "trace-model-1",
                "channel": "cli",
                "chat_id": "direct",
                "intent_name": "weather",
                "selected_model": "weather-model",
                "reason": "intent_override:weather",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    monkeypatch.setattr("zen_claw.dashboard.server.Path.home", lambda: tmp_path)

    snapshot = build_dashboard_snapshot(cfg)

    assert snapshot["agent"]["model_routing_summary"]["total"] == 1
    assert snapshot["agent"]["model_routing_summary"]["latest_model"] == "weather-model"
    assert snapshot["agent"]["model_routing_summary"]["latest_reason"] == "intent_override:weather"
    assert snapshot["agent"]["model_routing_events"][0]["intent_name"] == "weather"
    assert snapshot["agent"]["recent_observability_events"][0]["kind"] == "model_routing"


def test_build_dashboard_snapshot_includes_knowledge_summary(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "knowledge" / "chroma").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "knowledge" / "notebooks.json").write_text(
        json.dumps(
            {
                "notebooks": [
                    {
                        "id": "default",
                        "name": "Default",
                        "created_at": "2026-03-10T10:00:00+00:00",
                        "doc_count": 3,
                    },
                    {
                        "id": "chat_uploads",
                        "name": "chat_uploads",
                        "created_at": "2026-03-10T10:05:00+00:00",
                        "doc_count": 0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "knowledge_cron.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 300,
                "job_id": "job-1",
                "job_name": "refresh knowledge",
                "knowledge_source": str(tmp_path / "docs"),
                "knowledge_notebook": "Default",
                "status": "ok",
                "documents": 3,
                "chunks_added": 7,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    monkeypatch.setattr("zen_claw.dashboard.server.Path.home", lambda: tmp_path)

    snapshot = build_dashboard_snapshot(cfg)

    assert snapshot["knowledge"]["total_notebooks"] == 2
    assert snapshot["knowledge"]["total_documents"] == 3
    assert snapshot["knowledge"]["non_empty_notebooks"] == 1
    assert snapshot["knowledge"]["chroma_store_present"] is True
    assert snapshot["knowledge"]["cron_runs_ok"] == 1
    assert snapshot["knowledge"]["cron_runs_error"] == 0
    assert snapshot["knowledge"]["recent_cron_runs"][0]["job_name"] == "refresh knowledge"
    assert snapshot["knowledge"]["notebooks"][0]["name"] == "chat_uploads"
    assert snapshot["agent"]["recent_observability_events"][0]["kind"] == "knowledge_cron"


def test_build_dashboard_snapshot_classifies_knowledge_related_cron_jobs(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "k1",
                        "name": "refresh knowledge",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {
                            "message": "ingest notebook docs",
                            "deliver": False,
                            "targetUrl": "http://127.0.0.1:18791/chat/upload",
                        },
                        "state": {"lastStatus": "ok"},
                    },
                    {
                        "id": "c1",
                        "name": "daily reminder",
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                        "payload": {"message": "hello", "deliver": True},
                        "state": {"lastStatus": "ok"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)

    assert snapshot["cron"]["webhook_jobs"] == 1
    assert snapshot["cron"]["knowledge_related_jobs"] == 1
    assert snapshot["cron"]["jobs"][0]["target_kind"] in {"webhook", "channel_delivery"}
    assert any(job["knowledge_related"] for job in snapshot["cron"]["jobs"])
