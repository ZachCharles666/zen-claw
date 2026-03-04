import json
from pathlib import Path

from zen_claw.node.service import NodeService


def test_node_service_register_heartbeat_and_task_flow(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")

    reg = svc.register_node(name="phone-a", platform="android", capabilities=["camera", "notify"])
    node_id = reg["node_id"]
    token = reg["token"]
    assert node_id
    assert token
    assert "token_expires_at_ms" in reg

    assert svc.heartbeat(node_id=node_id, token=token) is True
    assert svc.heartbeat(node_id=node_id, token="bad") is False

    task = svc.add_task(node_id=node_id, task_type="message.send", payload={"text": "hi"})
    assert task is not None
    task_id = task["task_id"]

    pulled = svc.pull_task(node_id=node_id, token=token)
    assert pulled is not None
    assert pulled["task_id"] == task_id
    assert pulled["status"] == "leased"

    assert svc.ack_task(node_id=node_id, token=token, task_id=task_id) is True
    assert svc.complete_task(
        node_id=node_id,
        token=token,
        task_id=task_id,
        ok=True,
        result={"done": True},
        error="",
    ) is True

    all_tasks = svc.list_tasks(node_id=node_id)
    assert len(all_tasks) == 1
    assert all_tasks[0]["status"] == "done"
    assert all_tasks[0]["result"]["done"] is True


def test_node_service_idempotency_and_capability_policy(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-b", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]

    first = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello"},
        idempotency_key="msg-001",
    )
    assert first is not None
    assert first.get("deduplicated") is not True

    duplicate = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello"},
        idempotency_key="msg-001",
    )
    assert duplicate is not None
    assert duplicate["task_id"] == first["task_id"]
    assert duplicate.get("deduplicated") is True

    denied = svc.add_task(
        node_id=node_id,
        task_type="browser.open",
        payload={"url": "http://127.0.0.1"},
    )
    assert denied is not None
    assert denied.get("ok") is False
    assert denied.get("error_code") == "node_capability_denied"


def test_node_service_rejects_oversized_payload(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-big-payload", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]

    oversized = {"data": "x" * (101 * 1024)}
    out = svc.add_task(node_id=node_id, task_type="message.send", payload=oversized)
    assert out is not None
    assert out.get("ok") is False
    assert out.get("error_code") == "node_payload_too_large"


def test_node_service_truncates_oversized_result(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-big-result", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    token = reg["token"]
    task = svc.add_task(node_id=node_id, task_type="message.send", payload={"text": "ok"})
    assert task is not None
    task_id = str(task["task_id"])
    assert svc.pull_task(node_id=node_id, token=token) is not None
    assert svc.ack_task(node_id=node_id, token=token, task_id=task_id) is True

    oversized_result = {"data": "y" * (101 * 1024)}
    ok = svc.complete_task(
        node_id=node_id,
        token=token,
        task_id=task_id,
        ok=True,
        result=oversized_result,
        error=None,
    )
    assert ok is True

    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    assert rows[0]["result"]["error_code"] == "node_result_too_large"


def test_node_service_idempotency_replay_conflict_and_window(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    svc._idempotency_window_sec = 1
    reg = svc.register_node(name="phone-idem", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]

    first = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello"},
        idempotency_key="k-1",
    )
    assert first is not None
    assert first.get("task_id")

    same = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello"},
        idempotency_key="k-1",
    )
    assert same is not None
    assert same.get("deduplicated") is True
    assert same.get("task_id") == first.get("task_id")

    conflict = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello-2"},
        idempotency_key="k-1",
    )
    assert conflict is not None
    assert conflict.get("ok") is False
    assert conflict.get("error_code") == "node_replay_conflict"
    assert conflict.get("existing_task_id") == first.get("task_id")

    data = svc._load()
    data["tasks"][0]["created_at_ms"] = 1
    svc._save()
    expired = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"text": "hello-3"},
        idempotency_key="k-1",
    )
    assert expired is not None
    assert expired.get("ok") is not False
    assert expired.get("task_id") != first.get("task_id")


def test_node_service_dsl_static_checks_block_risky_payloads(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-static", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]

    loop_denied = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "Please do this in a while true loop"},
    )
    assert loop_denied is not None
    assert loop_denied.get("ok") is False
    assert loop_denied.get("error_code") == "node_dsl_static_denied"
    assert any("loop_risk:" in str(v) for v in loop_denied.get("violations", []))

    override_denied = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hi", "kill_switch_enabled": False},
    )
    assert override_denied is not None
    assert override_denied.get("ok") is False
    assert override_denied.get("error_code") == "node_dsl_static_denied"
    assert any("forbidden_payload_key:kill_switch_enabled" == str(v) for v in override_denied.get("violations", []))

    reserved_channel_denied = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "system", "chat_id": "x", "text": "hello"},
    )
    assert reserved_channel_denied is not None
    assert reserved_channel_denied.get("ok") is False
    assert reserved_channel_denied.get("error_code") == "node_dsl_static_denied"
    assert "reserved_channel:system" in reserved_channel_denied.get("violations", [])


def test_node_service_policy_task_type_and_concurrency(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-c", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    token = reg["token"]

    policy = svc.update_policy(
        node_id=node_id,
        allowed_task_types=["message.*"],
        max_running_tasks=1,
        allow_gateway_tasks=False,
    )
    assert policy is not None
    assert policy["allow_gateway_tasks"] is False

    denied = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "x"})
    assert denied is not None
    assert denied.get("error_code") == "node_policy_denied"

    accepted = svc.add_task(node_id=node_id, task_type="message.send", payload={"text": "ok"})
    assert accepted is not None and accepted.get("task_id")

    first = svc.pull_task(node_id=node_id, token=token)
    assert first is not None
    second = svc.pull_task(node_id=node_id, token=token)
    assert second is None


def test_node_service_approval_flow(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-d", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]

    svc.update_policy(node_id=node_id, require_approval_task_types=["agent.*"])
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert task["status"] == "pending_approval"

    claimed = svc.claim_next_gateway_task()
    assert claimed is None

    assert svc.approve_task(task_id=task["task_id"], approved_by="ops", note="ok") is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "pending"
    assert rows[0]["approval"]["approved_by"] == "ops"

    claimed2 = svc.claim_next_gateway_task()
    assert claimed2 is not None
    assert claimed2["task_id"] == task["task_id"]

    svc2 = NodeService(tmp_path / "nodes2.json")
    reg2 = svc2.register_node(name="phone-e", platform="android", capabilities=["notify"])
    node2 = reg2["node_id"]
    svc2.update_policy(node_id=node2, require_approval_task_types=["agent.*"])
    t2 = svc2.add_task(node_id=node2, task_type="agent.prompt", payload={"prompt": "x"})
    assert t2 is not None
    assert svc2.reject_task(task_id=t2["task_id"], rejected_by="ops", reason="deny") is True
    rows2 = svc2.list_tasks(node_id=node2)
    assert rows2[0]["status"] == "rejected"


def test_node_service_approval_timeout_and_events(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-f", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(
        node_id=node_id,
        require_approval_task_types=["agent.*"],
        approval_timeout_sec=1,
    )
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert task["status"] == "pending_approval"
    expires_at = task["approval"]["expires_at_ms"]
    assert isinstance(expires_at, int)

    changed = svc.expire_pending_approvals(now_ms=expires_at + 1)
    assert changed == 1
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "rejected"
    assert rows[0]["error"] == "approval timeout"

    events = svc.list_approval_events(task_id=task["task_id"])
    actions = [e["action"] for e in events]
    assert "submitted" in actions
    assert "expired" in actions


def test_node_service_multi_approver_flow(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-g", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(
        node_id=node_id,
        require_approval_task_types=["agent.*"],
        approval_required_count=2,
    )
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert task["status"] == "pending_approval"

    assert svc.approve_task(task_id=task["task_id"], approved_by="ops-a", note="a") is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "pending_approval"

    assert svc.approve_task(task_id=task["task_id"], approved_by="ops-b", note="b") is True
    rows2 = svc.list_tasks(node_id=node_id)
    assert rows2[0]["status"] == "pending"


def test_node_service_approval_audit_verify_and_tamper_detect(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json", audit_secret="secret-1")
    reg = svc.register_node(name="phone-h", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(node_id=node_id, require_approval_task_types=["agent.*"])
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert svc.approve_task(task_id=task["task_id"], approved_by="ops", note="ok") is True

    good = svc.verify_approval_events()
    assert good["ok"] is True
    assert good["checked"] >= 2

    data = svc._load()
    data["approval_events"][0]["note"] = "tampered"
    svc._save()
    bad = svc.verify_approval_events()
    assert bad["ok"] is False


def test_node_service_immutable_audit_sink_and_sync(tmp_path: Path) -> None:
    sink = tmp_path / "immutable-audit"
    svc = NodeService(tmp_path / "nodes.json", immutable_events_dir=sink)
    reg = svc.register_node(name="phone-i", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(node_id=node_id, require_approval_task_types=["agent.*"])
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert svc.approve_task(task_id=task["task_id"], approved_by="ops", note="ok") is True

    files = list(sink.glob("*.json"))
    assert len(files) >= 2

    sync1 = svc.sync_approval_events_to_immutable()
    assert sync1["ok"] is True
    assert sync1["synced"] == 0


def test_node_service_token_rotate_revoke_and_expire(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-token", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    token = reg["token"]

    assert svc.heartbeat(node_id=node_id, token=token) is True
    assert svc.revoke_token(node_id=node_id) is True
    assert svc.heartbeat(node_id=node_id, token=token) is False

    rotated = svc.rotate_token(node_id=node_id, ttl_sec=1)
    assert rotated is not None
    new_token = rotated["token"]
    assert new_token != token
    assert svc.heartbeat(node_id=node_id, token=new_token) is True
    assert svc.heartbeat(node_id=node_id, token=token) is False

    data = svc._load()
    data["nodes"][node_id]["token_expires_at_ms"] = 1
    svc._save()
    assert svc.heartbeat(node_id=node_id, token=new_token) is False


def test_node_service_task_and_approval_event_have_trace_id(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-trace", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(node_id=node_id, require_approval_task_types=["agent.*"])
    task = svc.add_task(node_id=node_id, task_type="agent.prompt", payload={"prompt": "hello"})
    assert task is not None
    assert str(task.get("trace_id") or "")

    events = svc.list_approval_events(task_id=task["task_id"])
    assert events
    assert str(events[0].get("trace_id") or "") == str(task.get("trace_id") or "")


def test_node_service_scan_token_rotation_candidates_and_rotate(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    a = svc.register_node(name="a", platform="android", capabilities=["notify"])
    b = svc.register_node(name="b", platform="android", capabilities=["notify"])
    c = svc.register_node(name="c", platform="android", capabilities=["notify"])

    data = svc._load()
    now_ms = int(data["nodes"][a["node_id"]]["updated_at_ms"] or 0)
    data["nodes"][a["node_id"]]["token_expires_at_ms"] = now_ms + 30_000  # expiring in 30s
    data["nodes"][b["node_id"]]["token_expires_at_ms"] = now_ms - 1       # expired
    data["nodes"][c["node_id"]]["token_revoked"] = True                    # revoked
    svc._save()

    scan = svc.scan_token_rotation(within_sec=60, rotate=False)
    assert scan["ok"] is True
    reasons = {r["node_id"]: r["reason"] for r in scan["candidates"]}
    assert reasons[a["node_id"]] == "expiring_soon"
    assert reasons[b["node_id"]] == "expired"
    assert reasons[c["node_id"]] == "revoked"

    rotated = svc.scan_token_rotation(within_sec=60, rotate=True, ttl_sec=120)
    assert len(rotated["rotated"]) == 3
    row_by_id = {r["node_id"]: r for r in rotated["rotated"]}
    assert row_by_id[a["node_id"]]["token_expires_at_ms"] is not None


def test_node_service_remote_immutable_sink_retries_then_succeeds(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    svc._remote_s3_bucket = "bucket-a"
    svc._remote_s3_prefix = "audit/nodes"
    svc._remote_retry_max = 3
    svc._remote_retry_backoff_ms = 0

    class _NotFoundError(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "404"}}

    calls = {"put": 0}

    class _FakeS3:
        def head_object(self, **kwargs):
            raise _NotFoundError()

        def put_object(self, **kwargs):
            calls["put"] += 1
            if calls["put"] < 2:
                raise RuntimeError("transient")
            return {}

    svc._remote_s3_client = _FakeS3()
    event = {"hash": "h1", "event_id": "e1"}
    ok = svc._write_remote_immutable_event(event)
    assert ok is True
    assert calls["put"] == 2


def test_node_service_remote_immutable_sink_failure_writes_alert(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    svc._remote_s3_bucket = "bucket-a"
    svc._remote_s3_prefix = "audit/nodes"
    svc._remote_retry_max = 2
    svc._remote_retry_backoff_ms = 0

    class _NotFoundError(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "404"}}

    class _AlwaysFailS3:
        def head_object(self, **kwargs):
            raise _NotFoundError()

        def put_object(self, **kwargs):
            raise RuntimeError("hard-fail")

    svc._remote_s3_client = _AlwaysFailS3()
    event = {"hash": "h2", "event_id": "e2"}
    ok = svc._write_remote_immutable_event(event)
    assert ok is False
    assert svc.alert_log_path.exists() is True
    rows = [json.loads(x) for x in svc.alert_log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r.get("code") == "remote_immutable_write_failed" for r in rows)
