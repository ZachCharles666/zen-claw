import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli import commands
from zen_claw.cli.commands import app
from zen_claw.node.service import NodeService


def test_node_cli_register_and_task_flow(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        [
            "node",
            "register",
            "--name",
            "demo-node",
            "--platform",
            "android",
            "--capability",
            "camera",
            "--capability",
            "notify",
        ],
    )
    assert reg.exit_code == 0
    reg_data = json.loads(reg.output)
    node_id = reg_data["node_id"]
    token = reg_data["token"]

    hb = runner.invoke(app, ["node", "heartbeat", "--node-id", node_id, "--token", token])
    assert hb.exit_code == 0

    add = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "message.send",
            "--payload",
            "{\"text\":\"hello\"}",
            "--idempotency-key",
            "msg-001",
        ],
    )
    assert add.exit_code == 0
    task = json.loads(add.output)
    task_id = task["task_id"]

    pull = runner.invoke(app, ["node", "task", "pull", "--node-id", node_id, "--token", token])
    assert pull.exit_code == 0
    pulled = json.loads(pull.output)
    assert pulled["task_id"] == task_id

    ack = runner.invoke(
        app,
        ["node", "task", "ack", "--node-id", node_id, "--token", token, "--task-id", task_id],
    )
    assert ack.exit_code == 0

    result = runner.invoke(
        app,
        [
            "node",
            "task",
            "result",
            "--node-id",
            node_id,
            "--token",
            token,
            "--task-id",
            task_id,
            "--ok",
            "--result",
            "{\"sent\":true}",
        ],
    )
    assert result.exit_code == 0

    add_dup = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "message.send",
            "--payload",
            "{\"text\":\"hello\"}",
            "--idempotency-key",
            "msg-001",
        ],
    )
    assert add_dup.exit_code == 0
    add_dup_task = json.loads(add_dup.output)
    assert add_dup_task["task_id"] == task_id
    assert add_dup_task["deduplicated"] is True

    denied = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "browser.open",
            "--payload",
            "{\"url\":\"http://127.0.0.1\"}",
        ],
    )
    assert denied.exit_code == 1


def test_node_cli_policy_set_and_policy_denied(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "policy-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    node_id = json.loads(reg.output)["node_id"]

    set_policy = runner.invoke(
        app,
        [
            "node",
            "policy",
            "set",
            "--node-id",
            node_id,
            "--allow-task-type",
            "message.*",
            "--max-running-tasks",
            "1",
            "--deny-gateway-tasks",
        ],
    )
    assert set_policy.exit_code == 0
    data = json.loads(set_policy.output)
    assert data["allow_gateway_tasks"] is False

    show_policy = runner.invoke(app, ["node", "policy", "show", "--node-id", node_id])
    assert show_policy.exit_code == 0
    show = json.loads(show_policy.output)
    assert show["max_running_tasks"] == 1
    assert show["allowed_task_types"] == ["message.*"]

    denied = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "agent.prompt",
            "--payload",
            "{\"prompt\":\"hello\"}",
        ],
    )
    assert denied.exit_code == 1


def test_node_cli_task_add_dsl_static_denied(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "dsl-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    node_id = json.loads(reg.output)["node_id"]

    denied = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "agent.prompt",
            "--payload",
            "{\"prompt\":\"run this while true forever\"}",
        ],
    )
    assert denied.exit_code == 1
    assert "DSL static check failed" in denied.output


def test_node_cli_task_add_idempotency_replay_conflict(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "idem-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    node_id = json.loads(reg.output)["node_id"]

    first = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "message.send",
            "--payload",
            "{\"text\":\"hello\"}",
            "--idempotency-key",
            "k-1",
        ],
    )
    assert first.exit_code == 0

    conflict = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "message.send",
            "--payload",
            "{\"text\":\"hello-2\"}",
            "--idempotency-key",
            "k-1",
        ],
    )
    assert conflict.exit_code == 1
    assert "idempotency replay conflict" in conflict.output


def test_node_cli_approval_commands(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "approval-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    node_id = json.loads(reg.output)["node_id"]

    set_policy = runner.invoke(
        app,
        [
            "node",
            "policy",
            "set",
            "--node-id",
            node_id,
            "--require-approval-task-type",
            "agent.*",
            "--approval-timeout-sec",
            "60",
            "--approval-required-count",
            "2",
        ],
    )
    assert set_policy.exit_code == 0

    add = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "agent.prompt",
            "--payload",
            "{\"prompt\":\"hello\"}",
            "--required-capability",
            "notify",
        ],
    )
    assert add.exit_code == 0
    task_id = json.loads(add.output)["task_id"]

    approve = runner.invoke(
        app,
        ["node", "task", "approve", "--task-id", task_id, "--by", "ops", "--note", "looks safe"],
    )
    assert approve.exit_code == 0
    list_after_first = runner.invoke(app, ["node", "task", "list", "--node-id", node_id])
    assert list_after_first.exit_code == 0
    rows1 = json.loads(list_after_first.output)
    first_row = next(r for r in rows1 if r["task_id"] == task_id)
    assert first_row["status"] == "pending_approval"

    approve2 = runner.invoke(
        app,
        ["node", "task", "approve", "--task-id", task_id, "--by", "ops2", "--note", "second signoff"],
    )
    assert approve2.exit_code == 0
    list_after_second = runner.invoke(app, ["node", "task", "list", "--node-id", node_id])
    assert list_after_second.exit_code == 0
    rows2 = json.loads(list_after_second.output)
    second_row = next(r for r in rows2 if r["task_id"] == task_id)
    assert second_row["status"] == "pending"

    add2 = runner.invoke(
        app,
        [
            "node",
            "task",
            "add",
            "--node-id",
            node_id,
            "--type",
            "agent.prompt",
            "--payload",
            "{\"prompt\":\"world\"}",
            "--required-capability",
            "notify",
            "--idempotency-key",
            "appr-2",
        ],
    )
    assert add2.exit_code == 0
    task2 = json.loads(add2.output)["task_id"]
    reject = runner.invoke(
        app,
        ["node", "task", "reject", "--task-id", task2, "--by", "ops", "--reason", "blocked"],
    )
    assert reject.exit_code == 0

    approvals = runner.invoke(
        app,
        ["node", "task", "approvals", "--task-id", task_id],
    )
    assert approvals.exit_code == 0
    events = json.loads(approvals.output)
    assert any(e.get("action") == "submitted" for e in events)
    assert any(e.get("action") in {"approved", "approved_step"} for e in events)

    verify = runner.invoke(app, ["node", "task", "approvals-verify"])
    assert verify.exit_code == 0
    verify_data = json.loads(verify.output)
    assert verify_data["ok"] is True

    immutable_dir = tmp_path / "immutable"
    svc.immutable_events_dir = immutable_dir
    sync = runner.invoke(app, ["node", "task", "approvals-sync-immutable"])
    assert sync.exit_code == 0
    sync_data = json.loads(sync.output)
    assert sync_data["ok"] is True
    assert len(list(immutable_dir.glob("*.json"))) >= 1


def test_node_cli_token_commands(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "token-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    reg_data = json.loads(reg.output)
    node_id = reg_data["node_id"]
    old_token = reg_data["token"]

    show1 = runner.invoke(app, ["node", "token", "show", "--node-id", node_id])
    assert show1.exit_code == 0
    show_data = json.loads(show1.output)
    assert show_data["node_id"] == node_id
    assert show_data["token"]
    assert "***" in show_data["token"]

    rotate = runner.invoke(app, ["node", "token", "rotate", "--node-id", node_id, "--ttl-sec", "60"])
    assert rotate.exit_code == 0
    rotate_data = json.loads(rotate.output)
    assert rotate_data["node_id"] == node_id
    assert rotate_data["token"] != old_token

    revoke = runner.invoke(app, ["node", "token", "revoke", "--node-id", node_id])
    assert revoke.exit_code == 0

    show2 = runner.invoke(app, ["node", "token", "show", "--node-id", node_id, "--show-token"])
    assert show2.exit_code == 0
    show2_data = json.loads(show2.output)
    assert show2_data["token_revoked"] is True


def test_node_cli_token_scan_and_rotate(tmp_path: Path, monkeypatch) -> None:
    svc = NodeService(tmp_path / "nodes" / "state.json")
    monkeypatch.setattr(commands, "_node_service", lambda: svc)
    runner = CliRunner()

    reg = runner.invoke(
        app,
        ["node", "register", "--name", "scan-node", "--platform", "android", "--capability", "notify"],
    )
    assert reg.exit_code == 0
    node_id = json.loads(reg.output)["node_id"]

    data = svc._load()
    data["nodes"][node_id]["token_expires_at_ms"] = 1
    svc._save()

    scan = runner.invoke(app, ["node", "token", "scan", "--within-sec", "0"])
    assert scan.exit_code == 0
    scan_data = json.loads(scan.output)
    assert any(r.get("node_id") == node_id for r in scan_data.get("candidates", []))

    rotate = runner.invoke(app, ["node", "token", "scan", "--within-sec", "0", "--rotate", "--ttl-sec", "120"])
    assert rotate.exit_code == 0
    rotate_data = json.loads(rotate.output)
    assert any(r.get("node_id") == node_id for r in rotate_data.get("rotated", []))
