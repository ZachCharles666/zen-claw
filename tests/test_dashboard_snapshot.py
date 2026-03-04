import json
from pathlib import Path

from zen_claw.config.schema import Config
from zen_claw.dashboard.server import build_dashboard_snapshot


def test_build_dashboard_snapshot_includes_cron_sidecar_rate_limit(monkeypatch, tmp_path: Path) -> None:
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
                    "n1": {"name": "phone-a", "platform": "android", "status": "active", "last_seen_ms": 123},
                    "n2": {"name": "phone-b", "platform": "ios", "status": "inactive", "last_seen_ms": 88},
                },
                "tasks": [
                    {"task_id": "t1", "node_id": "n1", "task_type": "agent.prompt", "status": "pending"},
                    {"task_id": "t2", "node_id": "n1", "task_type": "agent.prompt", "status": "running"},
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


def test_build_dashboard_snapshot_detects_approval_chain_tamper(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr("zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: [])

    snapshot = build_dashboard_snapshot(cfg)
    assert snapshot["node"]["approval_chain_ok"] is False
    assert snapshot["node"]["approval_chain_checked"] == 0
    assert snapshot["node"]["approval_chain_error"] in {"approval hash mismatch", "approval chain broken"}


def test_build_dashboard_snapshot_node_details_include_latest_task(monkeypatch, tmp_path: Path) -> None:
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
                    {"task_id": "t1", "node_id": "n1", "task_type": "message.send", "status": "pending", "updated_at_ms": 10},
                    {"task_id": "t2", "node_id": "n1", "task_type": "agent.prompt", "status": "running", "updated_at_ms": 20},
                ],
                "approval_events": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: [])
    snapshot = build_dashboard_snapshot(cfg)
    row = snapshot["node"]["nodes"][0]
    assert row["allow_gateway_tasks"] is False
    assert row["max_running_tasks"] == 2
    assert row["approval_required_count"] == 2
    assert row["latest_task_type"] == "agent.prompt"
    assert row["latest_task_status"] == "running"
