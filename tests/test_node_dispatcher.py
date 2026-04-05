from pathlib import Path

import pytest

from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.events import OutboundMessage
from zen_claw.node.dispatcher import NodeTaskDispatcher
from zen_claw.node.service import NodeService


@pytest.mark.asyncio
async def test_node_dispatcher_runs_agent_prompt_task(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-a", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={
            "prompt": "say hi",
            "session_key": "node:test",
            "channel": "cli",
            "chat_id": "node-a",
        },
    )
    assert task is not None

    called: dict[str, str] = {}

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        called["prompt"] = prompt
        called["session_key"] = str(kwargs.get("session_key") or "")
        called["trace_id"] = str(kwargs.get("trace_id") or "")
        return "agent-ok"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did_work = await dispatcher.run_once()
    assert did_work is True
    assert called["prompt"] == "say hi"
    assert called["session_key"] == "node:test"
    assert called["trace_id"]
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    assert rows[0]["result"]["response"] == "agent-ok"
    assert outbound == []


@pytest.mark.asyncio
async def test_node_dispatcher_forwards_agent_id_to_prompt_callback(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-agent-id", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={
            "prompt": "say hi",
            "session_key": "node:test",
            "channel": "cli",
            "chat_id": "node-a",
            "agent_id": "alpha",
        },
    )
    assert task is not None

    called: dict[str, str] = {}

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        called["agent_id"] = str(kwargs.get("agent_id") or "")
        return "ok"

    async def publish_outbound(msg: OutboundMessage) -> None:
        return None

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did_work = await dispatcher.run_once()
    assert did_work is True
    assert called["agent_id"] == "alpha"


@pytest.mark.asyncio
async def test_node_dispatcher_message_send_task(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-b", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    task = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "cli", "chat_id": "123", "content": "hello"},
    )
    assert task is not None

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=lambda msg: None,  # type: ignore[arg-type]
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
    )
    captured: dict[str, object] = {}

    async def _fake_execute_request(*, request_payload, trace_id):  # type: ignore[no-untyped-def]
        captured["request_payload"] = request_payload
        captured["trace_id"] = trace_id
        return ToolResult.success("queued", sidecar_target=dispatcher._connector_sidecar.proxy_url)

    dispatcher._connector_sidecar.execute_request = _fake_execute_request  # type: ignore[method-assign]
    did_work = await dispatcher.run_once()
    assert did_work is True
    request_payload = dict(captured["request_payload"] or {})
    assert request_payload["url"] == "channel://cli/123"
    connector_meta = dict(request_payload["connector_meta"])
    assert connector_meta["connector_name"] == "cli"
    assert connector_meta["action"] == "connector.message.send"
    assert request_payload["gateway_signature"]
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    assert rows[0]["result"]["sent"] is True


@pytest.mark.asyncio
async def test_node_dispatcher_message_send_external_channel_requires_sidecar(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-b2", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    task = svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "telegram", "chat_id": "123", "content": "hello"},
    )
    assert task is not None

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=lambda msg: None,  # type: ignore[arg-type]
    )
    did_work = await dispatcher.run_once()
    assert did_work is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "error"
    assert "sidecar configuration" in str(rows[0]["error"] or "")


@pytest.mark.asyncio
async def test_node_dispatcher_marks_unsupported_task_error(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-c", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    task = svc.add_task(
        node_id=node_id,
        task_type="capture.photo",
        payload={"x": 1},
        required_capability="notify",
    )
    assert task is not None

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did_work = await dispatcher.run_once()
    assert did_work is False
    assert outbound == []

    # Task remains pending because current dispatcher only claims gateway task types.
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_node_dispatcher_respects_allow_gateway_tasks_policy(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-d", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(node_id=node_id, allow_gateway_tasks=False)
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello"},
        required_capability="notify",
    )
    assert task is not None

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did_work = await dispatcher.run_once()
    assert did_work is False
    assert outbound == []
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_node_dispatcher_waits_for_approval(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-f", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(node_id=node_id, require_approval_task_types=["agent.*"])
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello"},
        required_capability="notify",
    )
    assert task is not None
    assert task["status"] == "pending_approval"

    called = {"count": 0}

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        called["count"] += 1
        return "ok"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did1 = await dispatcher.run_once()
    assert did1 is False
    assert called["count"] == 0

    assert svc.approve_task(task_id=task["task_id"], approved_by="ops", note="approve") is True
    did2 = await dispatcher.run_once()
    assert did2 is True
    assert called["count"] == 1


@pytest.mark.asyncio
async def test_node_dispatcher_expires_approval_before_claim(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-g", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(
        node_id=node_id,
        require_approval_task_types=["agent.*"],
        approval_timeout_sec=0,
    )
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello"},
        required_capability="notify",
    )
    assert task is not None
    assert task["status"] == "pending_approval"

    # Force-expire by patching task approval deadline in storage snapshot.
    data = svc._load()
    data["tasks"][0]["approval"]["expires_at_ms"] = 1
    svc._save()

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    did = await dispatcher.run_once()
    assert did is False
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_node_dispatcher_waits_until_all_approvals_collected(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-h", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(
        node_id=node_id,
        require_approval_task_types=["agent.*"],
        approval_required_count=2,
    )
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello"},
        required_capability="notify",
    )
    assert task is not None

    calls = {"n": 0}

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        calls["n"] += 1
        return "ok"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )
    assert svc.approve_task(task_id=task["task_id"], approved_by="ops-a", note="a") is True
    did1 = await dispatcher.run_once()
    assert did1 is False
    assert calls["n"] == 0

    assert svc.approve_task(task_id=task["task_id"], approved_by="ops-b", note="b") is True
    did2 = await dispatcher.run_once()
    assert did2 is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_node_high_risk_task_e2e_approval_to_gateway_execution_and_audit_verify(
    tmp_path: Path,
) -> None:
    svc = NodeService(tmp_path / "nodes.json", audit_secret="sec-1")
    reg = svc.register_node(name="phone-z", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.update_policy(
        node_id=node_id,
        require_approval_task_types=["agent.*"],
        approval_required_count=2,
    )
    task = svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "high-risk flow check"},
        required_capability="notify",
    )
    assert task is not None
    assert task["status"] == "pending_approval"

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return f"handled:{prompt}"

    outbound: list[OutboundMessage] = []

    async def publish_outbound(msg: OutboundMessage) -> None:
        outbound.append(msg)

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
    )

    # Pending approval should block gateway execution.
    did0 = await dispatcher.run_once()
    assert did0 is False

    assert (
        svc.approve_task(task_id=task["task_id"], approved_by="ops-a", note="first signoff") is True
    )
    did1 = await dispatcher.run_once()
    assert did1 is False

    assert (
        svc.approve_task(task_id=task["task_id"], approved_by="ops-b", note="second signoff")
        is True
    )
    did2 = await dispatcher.run_once()
    assert did2 is True
    assert outbound == []

    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    assert rows[0]["result"]["response"].startswith("handled:")

    verify = svc.verify_approval_events()
    assert verify["ok"] is True
    # submitted + approved_step + approved
    assert verify["checked"] >= 3


@pytest.mark.asyncio
async def test_node_dispatcher_message_send_never_uses_local_publish_outbound(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-cb-a", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "telegram", "chat_id": "1", "content": "m1"},
    )
    svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "telegram", "chat_id": "1", "content": "m2"},
    )

    calls = {"n": 0}

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    async def publish_outbound(msg: OutboundMessage) -> None:
        calls["n"] += 1
        raise RuntimeError("downstream failed")

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
    )
    async def _fake_execute_request(*, request_payload, trace_id):  # type: ignore[no-untyped-def]
        del request_payload, trace_id
        return ToolResult.failure(
            ToolErrorKind.RUNTIME,
            "sidecar down",
            code="connector_sidecar_unavailable",
        )

    dispatcher._connector_sidecar.execute_request = _fake_execute_request  # type: ignore[method-assign]
    did1 = await dispatcher.run_once()
    did2 = await dispatcher.run_once()
    assert did1 is True and did2 is True
    assert calls["n"] == 0
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "error"
    assert rows[1]["status"] == "error"
    assert "sidecar down" in str(rows[1].get("error") or "")


@pytest.mark.asyncio
async def test_node_dispatcher_message_send_sidecar_result_isolated_per_channel(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-cb-b", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "telegram", "chat_id": "1", "content": "tg-1"},
    )
    svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "discord", "chat_id": "2", "content": "dc-1"},
    )

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    async def publish_outbound(msg: OutboundMessage) -> None:
        raise RuntimeError("should not be called")

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
    )
    async def _fake_execute_request(*, request_payload, trace_id):  # type: ignore[no-untyped-def]
        del trace_id
        if request_payload["connector_meta"]["connector_name"] == "telegram":
            return ToolResult.failure(ToolErrorKind.RUNTIME, "telegram down", code="sidecar_error")
        return ToolResult.success("ok")

    dispatcher._connector_sidecar.execute_request = _fake_execute_request  # type: ignore[method-assign]
    did1 = await dispatcher.run_once()
    did2 = await dispatcher.run_once()
    assert did1 is True and did2 is True
    rows = svc.list_tasks(node_id=node_id)
    by_content = {r["payload"]["content"]: r for r in rows}
    assert by_content["tg-1"]["status"] == "error"
    assert by_content["dc-1"]["status"] == "done"


@pytest.mark.asyncio
async def test_node_dispatcher_agent_prompt_deliver_requires_sidecar(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-cb-c", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello", "deliver": True, "reply_channel": "cli", "reply_chat_id": "1"},
    )

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "reply body"

    async def publish_outbound(msg: OutboundMessage) -> None:
        raise RuntimeError("should not be called")

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
        connector_proxy_url="",
    )
    did = await dispatcher.run_once()
    assert did is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "error"
    assert "sidecar configuration" in str(rows[0]["error"] or "")


@pytest.mark.asyncio
async def test_node_dispatcher_agent_prompt_deliver_uses_sidecar_when_configured(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-chaos-a", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.add_task(
        node_id=node_id,
        task_type="agent.prompt",
        payload={"prompt": "hello", "deliver": True, "reply_channel": "cli", "reply_chat_id": "1"},
    )

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "done"

    async def publish_outbound(msg: OutboundMessage) -> None:
        raise RuntimeError("should not be called")

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
    )
    captured: dict[str, object] = {}

    async def _fake_execute_request(*, request_payload, trace_id):  # type: ignore[no-untyped-def]
        captured["request_payload"] = request_payload
        captured["trace_id"] = trace_id
        return ToolResult.success("ok")

    dispatcher._connector_sidecar.execute_request = _fake_execute_request  # type: ignore[method-assign]
    did = await dispatcher.run_once()
    assert did is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    request_payload = dict(captured["request_payload"] or {})
    assert request_payload["url"] == "channel://cli/1"
    assert request_payload["connector_meta"]["identity"]["trace_id"]


@pytest.mark.asyncio
async def test_node_dispatcher_message_send_sidecar_payload_includes_gateway_envelope(tmp_path: Path) -> None:
    svc = NodeService(tmp_path / "nodes.json")
    reg = svc.register_node(name="phone-chaos-b", platform="android", capabilities=["notify"])
    node_id = reg["node_id"]
    svc.add_task(
        node_id=node_id,
        task_type="message.send",
        payload={"channel": "telegram", "chat_id": "1", "content": "m1"},
    )

    async def on_agent_prompt(prompt: str, **kwargs) -> str:
        return "unused"

    async def publish_outbound(msg: OutboundMessage) -> None:
        raise RuntimeError("should not be called")

    dispatcher = NodeTaskDispatcher(
        node_service=svc,
        on_agent_prompt=on_agent_prompt,
        publish_outbound=publish_outbound,
        connector_proxy_url="http://127.0.0.1:4499/v1/fetch",
    )
    captured: dict[str, object] = {}

    async def _fake_execute_request(*, request_payload, trace_id):  # type: ignore[no-untyped-def]
        captured["request_payload"] = request_payload
        captured["trace_id"] = trace_id
        return ToolResult.success("ok")

    dispatcher._connector_sidecar.execute_request = _fake_execute_request  # type: ignore[method-assign]
    did = await dispatcher.run_once()
    assert did is True
    rows = svc.list_tasks(node_id=node_id)
    assert rows[0]["status"] == "done"
    request_payload = dict(captured["request_payload"] or {})
    connector_meta = dict(request_payload["connector_meta"])
    identity = dict(connector_meta["identity"])
    assert identity["trace_id"]
    assert identity["channel"] == "system"
    assert request_payload["gateway_signature"]
