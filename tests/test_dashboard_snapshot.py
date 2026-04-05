import json
from datetime import UTC, datetime
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

    rows = {row["name"]: row for row in snapshot["channels"]["rows"]}
    assert rows["telegram"]["rbac_enabled"] is True
    assert rows["telegram"]["admins"] == 1
    assert rows["telegram"]["users"] == 2
    assert rows["telegram"]["transport"] == "polling_http"
    assert rows["telegram"]["inbound_mode"] == "polling"
    assert rows["telegram"]["alpha_contract_ready"] is True
    assert rows["telegram"]["alpha_contract_missing"] == []
    assert rows["slack"]["passive_capable"] is True
    runtime_rows = {row["channel_name"]: row for row in snapshot["channels"]["runtime_controls"]}
    assert runtime_rows["webchat"]["state"] in {"running", "stopped"}
    assert runtime_rows["webchat"]["apply_supported"] is True
    assert runtime_rows["slack"]["supported"] is False
    assert snapshot["channels"]["actions_total"] == 0
    assert snapshot["channels"]["alpha_ready"] == snapshot["channels"]["total"]
    assert snapshot["channels"]["alpha_gaps"] == 0
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
    (data_dir / "dashboard" / "knowledge_policy.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 250,
                "trace_id": "trace-policy-1",
                "event": "knowledge.policy.update",
                "policy_kind": "tenant",
                "tenant_id": "default",
                "target_id": "default",
                "before_store_backend": "",
                "after_store_backend": "memory",
                "actor": "api_key",
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
    assert len(events) == 4
    assert [event["kind"] for event in events] == [
        "policy_audit",
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
        (
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
            + "\n"
            + json.dumps(
                {
                    "at_ms": 200,
                    "trace_id": "trace-model-2",
                    "channel": "cli",
                    "chat_id": "direct",
                    "intent_name": "finance",
                    "selected_model": "weather-model",
                    "reason": "intent_override:weather",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "at_ms": 150,
                    "trace_id": "trace-model-3",
                    "channel": "cli",
                    "chat_id": "direct",
                    "intent_name": "chat",
                    "selected_model": "default-model",
                    "reason": "default",
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

    assert snapshot["agent"]["model_routing_summary"]["total"] == 3
    assert snapshot["agent"]["model_routing_summary"]["latest_model"] == "weather-model"
    assert snapshot["agent"]["model_routing_summary"]["latest_reason"] == "intent_override:weather"
    assert snapshot["agent"]["model_routing_events"][0]["intent_name"] == "weather"
    assert snapshot["agent"]["model_routing_summary"]["models"][0] == {
        "model": "weather-model",
        "count": 2,
    }
    assert snapshot["agent"]["model_routing_summary"]["reasons"][0] == {
        "reason": "intent_override:weather",
        "count": 2,
    }
    assert snapshot["agent"]["model_routing_summary"]["intents"][0] == {
        "intent": "weather",
        "count": 1,
    }
    assert snapshot["agent"]["model_routing_summary"]["channels"][0] == {
        "channel": "cli",
        "count": 3,
    }
    assert snapshot["agent"]["recent_observability_events"][0]["kind"] == "model_routing"


def test_build_dashboard_snapshot_includes_agent_profiles(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                        "enable_planning": False,
                        "routing_keywords": ["finance", "bank campaign"],
                        "allowed_tools": ["rag_retrieve", "content_gen"],
                        "denied_tools": ["exec"],
                    }
                },
            }
        }
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["agent"]["profiles_total"] == 2
    assert snapshot["agent"]["registered_profiles"] == 2
    assert snapshot["agent"]["profiles"][1]["agent_id"] == "finance_writer"
    assert snapshot["agent"]["profiles"][1]["model"] == "deepseek-chat"
    assert snapshot["agent"]["profiles"][1]["planning_enabled"] is False
    assert snapshot["agent"]["profiles"][1]["routing_keywords"] == ["finance", "bank campaign"]
    assert snapshot["agent"]["profiles"][1]["routing_keywords_count"] == 2


def test_build_dashboard_snapshot_includes_agent_actions(
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
    (data_dir / "dashboard" / "agent_actions.log.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "at_ms": 200,
                        "trace_id": "trace-agent-2",
                        "event": "agent.profile.updated",
                        "agent_id": "finance_writer",
                        "action": "planning_disable",
                        "planning_enabled": False,
                        "actor": "api_key",
                        "detail": "",
                    }
                ),
                json.dumps(
                    {
                        "at_ms": 100,
                        "trace_id": "trace-agent-1",
                        "event": "agent.profile.updated",
                        "agent_id": "default",
                        "action": "planning_enable",
                        "planning_enabled": True,
                        "actor": "api_key",
                        "detail": "",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["agent"]["actions_total"] == 2
    assert snapshot["agent"]["recent_actions"][0]["agent_id"] == "finance_writer"
    assert snapshot["agent"]["recent_actions"][0]["action"] == "planning_disable"
    assert snapshot["agent"]["pending_reload"] is True
    assert snapshot["agent"]["pending_reload_count"] == 2
    assert snapshot["ops"]["status"] == "attention"
    assert "agents pending apply: 2" in snapshot["ops"]["notes"]
    assert snapshot["ops"]["pending_apply_total"] == 2
    assert snapshot["ops"]["pending_apply_rows"][0]["surface"] == "agent"
    assert "execution_mode" in snapshot["ops"]["pending_apply_rows"][0]
    assert any(
        section["key"] == "pending_apply_audit_only"
        for section in snapshot["ops"]["attention_sections"]
    )
    assert snapshot["ops"]["recent_activity"][0]["surface"] == "agent"
    assert any(section["key"] == "pending_apply" for section in snapshot["ops"]["attention_sections"])


def test_build_dashboard_snapshot_includes_agent_state_layers(
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
    (data_dir / "dashboard" / "agent_execution.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 123,
                "trace_id": "trace-exec-1",
                "channel": "cli",
                "chat_id": "direct",
                "intent_name": "time",
                "routing_decision": {
                    "action": "execute",
                    "route_status": "direct_success",
                    "routing_stage": "gate1_execute",
                    "entered_gate3": False,
                },
                "arbitration_decision": {"kind": ""},
                "execution_intent": {
                    "path_type": "direct",
                    "execution_mode": "runtime_direct",
                    "decision_source": "intent_router",
                    "operation_kind": "read_only",
                    "concurrency_class": "parallel_safe",
                    "approval_required": False,
                },
                "execution_result": {
                    "status": "succeeded",
                    "failure_classification": "",
                    "final_content_preview": "纽约当前时间：09:00",
                    "trace_summary": {
                        "selected_model": "",
                        "model_reason": "",
                        "reflections_used": 1,
                        "reflection_triggered": True,
                        "tool_failures": 1,
                        "fallback_used": True,
                        "fallback_reason": "fallback_model:primary-model",
                        "reload_triggered": False,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)

    assert snapshot["agent"]["config_state"]["model"] == snapshot["agent"]["model"]
    assert snapshot["agent"]["runtime_state"]["intent_router_summary"] == snapshot["agent"]["intent_router_summary"]
    assert snapshot["agent"]["runtime_state"]["execution_summary"]["total"] == 1
    assert snapshot["agent"]["runtime_state"]["execution_summary"]["reflected_runs"] == 1
    assert snapshot["agent"]["runtime_state"]["execution_summary"]["fallback_runs"] == 1
    assert snapshot["agent"]["runtime_state"]["execution_summary"]["tool_failures"] == 1
    assert snapshot["agent"]["runtime_state"]["execution_events"][0]["execution_intent"]["path_type"] == "direct"
    assert snapshot["agent"]["operator_state"]["profiles_total"] == snapshot["agent"]["profiles_total"]
    assert snapshot["ops"]["agent_execution_total"] == 1
    assert snapshot["ops"]["agent_execution_reflected"] == 1
    assert snapshot["ops"]["agent_execution_fallback"] == 1
    assert snapshot["ops"]["recent_activity"][0]["surface"] == "agent_execution"
    assert "reflections=1" in snapshot["ops"]["recent_activity"][0]["detail"]


def test_build_dashboard_snapshot_clears_pending_agent_reload_after_apply(
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
    (data_dir / "dashboard" / "agent_actions.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 700,
                "trace_id": "trace-agent",
                "event": "agent.profile.updated",
                "agent_id": "finance_writer",
                "action": "model_update",
                "planning_enabled": True,
                "actor": "api_key",
                "detail": "deepseek-chat -> gpt-4.1-mini",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "agent_apply.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 800,
                "trace_id": "trace-apply",
                "event": "agent.config.applied",
                "actor": "api_key",
                "note": "operator_confirmed_reload",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["agent"]["pending_reload"] is False
    assert snapshot["agent"]["pending_reload_count"] == 0
    assert snapshot["agent"]["last_apply"]["event"] == "agent.config.applied"


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
    (data_dir / "tenants" / "11111111-1111-1111-1111-111111111111").mkdir(parents=True, exist_ok=True)
    (data_dir / "tenants" / "11111111-1111-1111-1111-111111111111" / "tenant.json").write_text(
        json.dumps(
            {
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "name": "Acme",
                "created_at": 1,
                "enabled": True,
                "store_backend": "memory",
            }
        ),
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
                        "store_backend": "memory",
                    },
                    {
                        "id": "chat_uploads",
                        "name": "chat_uploads",
                        "created_at": "2026-03-10T10:05:00+00:00",
                        "doc_count": 0,
                        "store_backend": "",
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
    assert snapshot["knowledge"]["notebooks"][1]["store_backend"] == "memory"
    assert snapshot["knowledge"]["tenant_policies"][0]["store_backend"] == "memory"
    assert snapshot["knowledge"]["backend_diagnostics"]["configured_backend"] == "chroma"
    assert snapshot["knowledge"]["backend_diagnostics"]["all_healthy"] is True
    assert snapshot["knowledge"]["backend_diagnostics"]["detected_backends"][0]["backend"] == "chroma"
    assert snapshot["knowledge"]["recent_policy_changes"] == []
    assert snapshot["agent"]["recent_observability_events"][0]["kind"] == "knowledge_cron"


def test_build_dashboard_snapshot_includes_recent_knowledge_policy_changes(
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
    (data_dir / "dashboard" / "knowledge_policy.log.jsonl").write_text(
        (
            json.dumps(
                {
                    "at_ms": 300,
                    "trace_id": "trace-tenant",
                    "event": "knowledge.policy.update",
                    "policy_kind": "tenant",
                    "tenant_id": "default",
                    "target_id": "default",
                    "before_store_backend": "",
                    "after_store_backend": "memory",
                    "actor": "api_key",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "at_ms": 400,
                    "trace_id": "trace-notebook",
                    "event": "knowledge.policy.update",
                    "policy_kind": "notebook",
                    "tenant_id": "default",
                    "target_id": "finance_kb",
                    "before_store_backend": "memory",
                    "after_store_backend": "chroma",
                    "actor": "api_key",
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

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["knowledge"]["policy_changes_total"] == 2
    assert snapshot["knowledge"]["recent_policy_changes"][0]["policy_kind"] == "notebook"
    assert snapshot["knowledge"]["recent_policy_changes"][1]["policy_kind"] == "tenant"


def test_build_dashboard_snapshot_includes_skill_actions(monkeypatch, tmp_path: Path) -> None:
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
    (data_dir / "dashboard" / "skills_actions.log.jsonl").write_text(
        (
            json.dumps(
                {
                    "at_ms": 500,
                    "trace_id": "trace-disable",
                    "event": "skills.state.change",
                    "skill_name": "alpha",
                    "action": "disable",
                    "enabled": False,
                    "actor": "api_key",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "at_ms": 600,
                    "trace_id": "trace-enable",
                    "event": "skills.state.change",
                    "skill_name": "alpha",
                    "action": "enable",
                    "enabled": True,
                    "actor": "api_key",
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

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["skills"]["actions_total"] == 2
    assert snapshot["skills"]["recent_actions"][0]["skill_name"] == "alpha"
    assert snapshot["skills"]["recent_actions"][0]["action"] == "enable"


def test_build_dashboard_snapshot_includes_skill_checks(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    skill_dir = cfg.workspace_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "tests").mkdir(parents=True, exist_ok=True)
    (skill_dir / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"trust":"trusted","runtime_contract":{"intent":"assist","intent_mode":"skill_first"}}',
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "skills_checks.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 700,
                "trace_id": "trace-check",
                "event": "skills.preflight",
                "skill_name": "alpha",
                "ok": True,
                "manifest_ok": True,
                "integrity_ok": True,
                "tests_present": True,
                "detail": "manifest/integrity checks passed",
                "actor": "api_key",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["skills"]["checks_total"] == 1
    assert snapshot["skills"]["failed_checks"] == 0
    assert snapshot["skills"]["recent_checks"][0]["skill_name"] == "alpha"
    assert snapshot["skills"]["recent_checks"][0]["ok"] is True
    assert snapshot["skills"]["rows"][0]["last_check"]["detail"] == "manifest/integrity checks passed"


def test_build_dashboard_snapshot_includes_skill_exports(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    skill_dir = cfg.workspace_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"trust":"trusted"}',
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "skills_exports.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 710,
                "trace_id": "trace-export",
                "event": "skills.export",
                "skill_name": "alpha",
                "out_path": str(tmp_path / "ws" / ".zen-claw" / "exports" / "alpha.zip"),
                "message": "exported skill: alpha",
                "actor": "api_key",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["ops"]["status"] == "attention"
    assert "no recent model routing events" in snapshot["ops"]["notes"]
    assert snapshot["skills"]["exports_total"] == 1
    assert snapshot["skills"]["recent_exports"][0]["skill_name"] == "alpha"


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


def test_build_dashboard_snapshot_includes_skills_summary(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    skill_dir = cfg.workspace_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "tests").mkdir(parents=True, exist_ok=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "version": "1.0.0",
                "description": "alpha skill",
                "permissions": ["read_file"],
                "scopes": ["filesystem"],
                "trust": "trusted",
                "runtime_contract": {"intent": "assist", "intent_mode": "skill_first"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "cron").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "channels").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "nodes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["skills"]["total"] == 1
    assert snapshot["skills"]["enabled"] == 1
    assert snapshot["skills"]["enforce_ready"] == 1
    assert snapshot["skills"]["tested"] == 1
    assert snapshot["skills"]["trusted"] == 1
    assert snapshot["skills"]["runtime_intent"] == 1
    assert snapshot["skills"]["trust_breakdown"]["trusted"] == 1
    assert snapshot["skills"]["runtime_mode_breakdown"]["skill_first"] == 1
    assert snapshot["skills"]["rows"][0]["name"] == "alpha"
    assert snapshot["skills"]["rows"][0]["tests_present"] is True


def test_build_dashboard_snapshot_uses_shared_channel_registry(monkeypatch, tmp_path: Path) -> None:
    cfg = Config()
    cfg.channels.slack.enabled = True
    cfg.channels.slack.admins = ["s-admin"]
    cfg.channels.webchat.enabled = True
    cfg.channels.webchat.users = ["web-user"]
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    rows = {row["name"]: row for row in snapshot["channels"]["rows"]}
    assert rows["slack"]["enabled"] is True
    assert rows["slack"]["admins"] == 1
    assert rows["webchat"]["users"] == 1
    assert rows["slack"]["webhook_backed"] is True
    assert rows["slack"]["verify_mode"] == "socket_mode_or_webhook_signature"
    assert rows["slack"]["media_supported"] is True
    assert rows["slack"]["runtime_control_mode"] == "audit_only"
    assert rows["slack"]["alpha_contract_ready"] is True
    assert rows["webchat"]["transport"] == "local_web"


def test_build_dashboard_snapshot_includes_channel_actions(monkeypatch, tmp_path: Path) -> None:
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
    (data_dir / "dashboard" / "channel_actions.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 700,
                "trace_id": "trace-channel",
                "event": "channels.state.change",
                "channel_name": "slack",
                "action": "disable",
                "enabled": False,
                "actor": "api_key",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["channels"]["actions_total"] == 1
    assert snapshot["channels"]["recent_actions"][0]["channel_name"] == "slack"
    assert snapshot["channels"]["pending_reload"] is True
    assert snapshot["channels"]["pending_reload_count"] == 1


def test_build_dashboard_snapshot_includes_crawler_summary(monkeypatch, tmp_path: Path) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "crawler").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "crawler" / "sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "news",
                        "url": "https://example.com/news",
                        "notebook": "crawler_kb",
                        "use_browser": True,
                        "selector": ".body",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "crawler_sources.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 900,
                "event": "crawler.source.upsert",
                "source_name": "news",
                "source_url": "https://example.com/news",
                "notebook_id": "crawler_kb",
                "actor": "api_key",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "crawler_runs.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 950,
                "event": "crawler.run",
                "source_name": "news",
                "source_url": "https://example.com/news",
                "status": "ok",
                "change_status": "updated",
                "chunks_added": 4,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["crawler"]["total_sources"] == 1
    assert snapshot["crawler"]["browser_sources"] == 1
    assert snapshot["crawler"]["runs_total"] == 1
    assert snapshot["crawler"]["failed_runs"] == 0
    assert snapshot["crawler"]["sources"][0]["name"] == "news"
    assert snapshot["crawler"]["recent_runs"][0]["change_status"] == "updated"


def test_build_dashboard_snapshot_clears_pending_channel_reload_after_apply(
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
    (data_dir / "dashboard" / "channel_actions.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 700,
                "trace_id": "trace-channel",
                "event": "channels.state.change",
                "channel_name": "slack",
                "action": "disable",
                "enabled": False,
                "actor": "api_key",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "channel_apply.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 800,
                "trace_id": "trace-apply",
                "event": "channels.config.applied",
                "actor": "api_key",
                "note": "operator_confirmed_reload",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["channels"]["pending_reload"] is False
    assert snapshot["channels"]["pending_reload_count"] == 0
    assert snapshot["channels"]["last_apply"]["event"] == "channels.config.applied"


def test_build_dashboard_snapshot_includes_zero_trust_security_fields(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config.model_validate(
        {
            "agents": {
                "profiles": {
                    "dev_shell": {
                        "dev_profile": True,
                        "trusted_local_only": True,
                        "allowed_channels": ["cli"],
                        "allowed_tools": ["read_file"],
                    }
                }
            },
            "tools": {
                "policy": {
                    "production_hardening": True,
                    "legacy_compat": False,
                    "trusted_local_channels": ["cli", "system"],
                }
            },
        }
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "audit_logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["security"]["production_hardening"] is True
    assert snapshot["security"]["legacy_compat"] is False
    assert snapshot["security"]["local_fallback_enabled"] is False
    assert snapshot["security"]["trusted_local_channels"] == ["cli", "system"]
    assert snapshot["security"]["local_identity_channels"] == ["cli", "system"]
    assert snapshot["security"]["dev_profile_total"] == 1
    assert snapshot["security"]["dev_profiles"] == ["dev_shell"]
    assert snapshot["security"]["connector_write_policy_configured"] is False
    assert snapshot["security"]["connector_write_profiles"] == []
    assert snapshot["security"]["message_write_profiles"] == []
    assert snapshot["security"]["webhook_write_profiles"] == []
    assert snapshot["security"]["capability_policy_families"] == []
    assert snapshot["security"]["sensitive_capability_profiles"]["message.send"] == []
    assert snapshot["security"]["legacy_tool_policy_paths"] is False
    assert snapshot["security"]["connector_write_recent_events"] == []
    assert snapshot["security"]["security_audit_recent_events"] == []
    assert snapshot["security"]["security_approval_recent_events"] == []
    assert snapshot["security"]["security_query_coverage"] == {
        "events_total": 0,
        "with_capability": 0,
        "with_resource_scope": 0,
        "with_policy_snapshot_hash": 0,
        "with_request_hash": 0,
        "with_decision_id": 0,
        "with_gateway_instance": 0,
        "with_trust_level": 0,
    }
    assert snapshot["security"]["webhook_write_recent_events"] == []
    assert snapshot["security"]["channel_outbound_local_only"] == []
    assert "telegram" in snapshot["security"]["channel_outbound_isolated"]
    assert snapshot["security"]["channel_outbound_blocked"] == []
    assert snapshot["security"]["channel_outbound_recent_events"] == []
    assert snapshot["security"]["write_isolation_paths"] == [
        "connector.record.create",
        "connector.record.update",
        "connector.message.send",
        "connector.webhook.trigger",
    ]
    assert snapshot["security"]["audit_chain_ok"] is True
    assert snapshot["security"]["audit_chain_checked"] == 0
    assert snapshot["security"]["replay_consistency_ok"] is True
    assert snapshot["security"]["replay_consistency_checked"] == 0


def test_build_dashboard_snapshot_includes_connector_security_summary(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config.model_validate(
        {
            "agents": {"profiles": {"ops": {"allowed_tools": ["social_platform_post"]}}},
            "tools": {
                "policy": {
                    "connector_allowed_names": ["moltbook"],
                    "connector_allowed_actions": ["connector.record.create"],
                    "connector_allowed_target_resources": ["moltbook.posts"],
                }
            },
        }
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "audit_logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "audit_logs" / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-04-05T00:00:00+00:00",
                "trace_id": "trace-1",
                "prev_hash": "",
                "event_type": "tool.denied",
                "tool": "social_platform_post",
                "error_code": "resource_scope_connector_unconfigured",
                "hash": "placeholder",
                "signature": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["security"]["connector_write_policy_configured"] is True
    assert snapshot["security"]["connector_write_profiles"] == ["ops"]
    assert snapshot["security"]["message_write_profiles"] == []
    assert snapshot["security"]["webhook_write_profiles"] == []
    assert snapshot["security"]["connector_write_recent_events"][0]["tool"] == "social_platform_post"
    assert "connector" in snapshot["security"]["capability_policy_families"]
    assert snapshot["security"]["capability_policy_recent_events"][0]["capability"] in {
        "",
        "connector.record.create",
        "connector.message.send",
    }


def test_build_dashboard_snapshot_includes_security_query_fields(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "audit_logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    audit_path = data_dir / "audit_logs" / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-04-05T00:00:00+00:00",
                "trace_id": "trace-sec-1",
                "prev_hash": "",
                "event_type": "tool.executed",
                "tool": "social_platform_post",
                "capability": "connector.record.create",
                "resource_scope": {"connector_name": "moltbook"},
                "policy_snapshot_hash": "policy-1",
                "request_hash": "req-1",
                "gateway_instance": "gw-1",
                "sidecar_target": "http://127.0.0.1:4499/v1/fetch",
                "identity": {
                    "channel": "cli",
                    "sender_id": "user-1",
                    "chat_id": "chat-1",
                    "tenant_id": "default",
                    "workspace_id": "ws-1",
                    "agent_profile": "ops",
                    "role": "operator",
                    "trust_level": "trusted_local",
                },
                "hash": "placeholder-1",
                "signature": "",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-04-05T00:01:00+00:00",
                "trace_id": "trace-sec-2",
                "prev_hash": "placeholder-1",
                "event_type": "approval.approved",
                "approval": {
                    "approval_id": "APR-1",
                    "capability": "exec.run",
                    "request_hash": "req-2",
                    "policy_snapshot_hash": "policy-2",
                    "actor": "ops_reviewer",
                },
                "identity": {
                    "channel": "webchat",
                    "sender_id": "user-2",
                    "chat_id": "chat-2",
                    "tenant_id": "default",
                    "workspace_id": "ws-2",
                    "agent_profile": "default",
                    "role": "operator",
                    "trust_level": "remote_untrusted",
                    "gateway_instance": "gw-2",
                },
                "resource_scope": {"working_dir": "E:/nano-claw-public"},
                "hash": "placeholder-2",
                "signature": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["security"]["security_audit_recent_events"][0]["trace_id"] == "trace-sec-2"
    assert snapshot["security"]["security_audit_recent_events"][0]["decision_id"] == "APR-1"
    assert snapshot["security"]["security_audit_recent_events"][0]["request_hash"] == "req-2"
    assert snapshot["security"]["security_audit_recent_events"][0]["policy_snapshot_hash"] == "policy-2"
    assert snapshot["security"]["security_audit_recent_events"][0]["gateway_instance"] == "gw-2"
    assert snapshot["security"]["security_audit_recent_events"][0]["trust_level"] == "remote_untrusted"
    assert snapshot["security"]["security_approval_recent_events"][0]["decision_id"] == "APR-1"
    assert snapshot["security"]["security_approval_recent_events"][0]["actor"] == "ops_reviewer"
    assert snapshot["security"]["security_query_coverage"]["events_total"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_capability"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_resource_scope"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_policy_snapshot_hash"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_request_hash"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_decision_id"] == 1
    assert snapshot["security"]["security_query_coverage"]["with_gateway_instance"] == 2
    assert snapshot["security"]["security_query_coverage"]["with_trust_level"] == 2


def test_build_dashboard_snapshot_includes_channel_outbound_isolation_summary(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = Config.model_validate(
        {
            "channels": {"discord": {"enabled": True}, "webchat": {"enabled": True}},
            "tools": {
                "network": {"connector": {"proxy_url": "http://127.0.0.1:4499/v1/fetch"}},
                "policy": {
                    "connector_allowed_names": ["discord"],
                    "connector_allowed_actions": ["connector.message.send"],
                    "connector_allowed_target_resources": ["channel.discord.message"],
                },
            },
        }
    )
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "audit_logs").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "audit_logs" / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-04-05T00:00:00+00:00",
                "trace_id": "trace-out-1",
                "prev_hash": "",
                "event_type": "message.outbound.executed",
                "channel": "discord",
                "action": "connector.message.send",
                "isolation_mode": "isolated_external",
                "sidecar_target": "http://127.0.0.1:4499/v1/fetch",
                "hash": "placeholder",
                "signature": "",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-04-05T00:01:00+00:00",
                "trace_id": "trace-out-2",
                "prev_hash": "placeholder",
                "event_type": "message.outbound.direct_blocked",
                "channel": "discord",
                "error_code": "external_channel_direct_send_blocked",
                "send_path": "send",
                "isolation_mode": "guardrail_blocked",
                "hash": "placeholder-2",
                "signature": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)
    rows = {row["name"]: row for row in snapshot["channels"]["rows"]}
    assert rows["discord"]["outbound_isolation_mode"] == "isolated_external"
    assert rows["webchat"]["outbound_isolation_mode"] == "isolated_external"
    assert "discord" in snapshot["security"]["channel_outbound_isolated"]
    assert "webchat" in snapshot["security"]["channel_outbound_isolated"]
    assert "webchat" not in snapshot["security"]["channel_outbound_local_only"]
    assert "discord" in snapshot["security"]["external_channel_guardrail_coverage"]
    assert snapshot["security"]["legacy_direct_send_exposed"] is False
    assert snapshot["security"]["channel_outbound_recent_events"][0]["channel"] == "discord"
    assert (
        snapshot["security"]["channel_outbound_guardrail_recent_events"][0]["event_type"]
        == "message.outbound.direct_blocked"
    )
