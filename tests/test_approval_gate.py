"""Tests for ApprovalGate — covers request creation, approve/deny/expire lifecycle, and persistence."""

import asyncio
from pathlib import Path

from zen_claw.agent.approval_gate import ApprovalGate, ApprovalStatus

# ── helpers ───────────────────────────────────────────────────────────────────


def _gate(tmp_path: Path, **kwargs) -> ApprovalGate:
    return ApprovalGate(data_dir=tmp_path, **kwargs)


# ── tests: is_sensitive ───────────────────────────────────────────────────────


def test_is_sensitive_returns_true_for_write_file(tmp_path: Path):
    gate = _gate(tmp_path)
    assert gate.is_sensitive("write_file") is True


def test_is_sensitive_returns_true_for_exec(tmp_path: Path):
    gate = _gate(tmp_path)
    assert gate.is_sensitive("exec") is True


def test_is_sensitive_returns_false_for_read_file(tmp_path: Path):
    gate = _gate(tmp_path)
    assert gate.is_sensitive("read_file") is False


def test_is_sensitive_custom_set(tmp_path: Path):
    gate = _gate(tmp_path, sensitive_tools=frozenset({"my_secret_tool"}))
    assert gate.is_sensitive("my_secret_tool") is True
    assert gate.is_sensitive("write_file") is False  # not in custom set


# ── tests: request_approval ───────────────────────────────────────────────────


async def test_request_approval_creates_pending_record(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "write_file", {"path": "/etc/hosts"})
    assert rec.status == ApprovalStatus.PENDING
    assert rec.tool_name == "write_file"
    assert rec.session_id == "sess-1"
    assert len(rec.approval_id) == 8


async def test_request_approval_persists_to_disk(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "exec", {"cmd": "rm -rf /"})
    # Reload from disk
    gate2 = _gate(tmp_path)
    loaded = gate2.get(rec.approval_id)
    assert loaded is not None
    assert loaded.tool_name == "exec"
    assert loaded.status == ApprovalStatus.PENDING


async def test_request_approval_message_contains_tool_name(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "write_file", {"path": "/tmp/out.txt"})
    msg = rec.format_request_message()
    assert "`write_file`" in msg
    assert rec.approval_id in msg
    assert "/approve" in msg
    assert "/deny" in msg


# ── tests: approve / deny ─────────────────────────────────────────────────────


async def test_approve_updates_status(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "exec", {"cmd": "ls"})
    approved = gate.approve(rec.approval_id)
    assert approved is not None
    assert approved.status == ApprovalStatus.APPROVED
    assert approved.decided_at is not None


async def test_deny_updates_status(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "exec", {"cmd": "ls"})
    denied = gate.deny(rec.approval_id)
    assert denied is not None
    assert denied.status == ApprovalStatus.DENIED


def test_approve_unknown_id_returns_none(tmp_path: Path):
    gate = _gate(tmp_path)
    assert gate.approve("XXXXXXXX") is None


async def test_double_deny_returns_none(tmp_path: Path):
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "exec", {})
    gate.deny(rec.approval_id)
    assert gate.deny(rec.approval_id) is None  # already decided


# ── tests: expiry ─────────────────────────────────────────────────────────────


async def test_expired_records_show_as_expired(tmp_path: Path):
    gate = _gate(tmp_path, ttl_seconds=0)  # instant TTL
    rec = await gate.request_approval("sess-1", "spawn", {})
    # Trigger expiry check
    await asyncio.sleep(0.01)
    gate._expire_old()
    reloaded = gate.get(rec.approval_id)
    # After expiry, get() returns None (not PENDING)
    assert reloaded is None or reloaded.status == ApprovalStatus.EXPIRED


# ── tests: list_pending ───────────────────────────────────────────────────────


async def test_list_pending_returns_only_pending(tmp_path: Path):
    gate = _gate(tmp_path)
    r1 = await gate.request_approval("sess-1", "exec", {})
    r2 = await gate.request_approval("sess-1", "write_file", {})
    gate.approve(r1.approval_id)  # r1 is now APPROVED

    pending = gate.list_pending()
    assert len(pending) == 1
    assert pending[0].approval_id == r2.approval_id


async def test_list_pending_filters_by_session(tmp_path: Path):
    gate = _gate(tmp_path)
    await gate.request_approval("sess-A", "exec", {})
    await gate.request_approval("sess-B", "spawn", {})

    pending_a = gate.list_pending(session_id="sess-A")
    assert all(r.session_id == "sess-A" for r in pending_a)
    assert len(pending_a) == 1


# ── tests: AgentLoop /approve integration ────────────────────────────────────


async def test_agent_loop_approve_command_returns_confirmation(tmp_path: Path):
    """AgentLoop /approve command should return a confirmation without calling LLM."""
    from unittest.mock import AsyncMock, MagicMock

    from zen_claw.agent.loop import AgentLoop
    from zen_claw.bus.events import InboundMessage

    gate = _gate(tmp_path)
    rec = await gate.request_approval("cli:direct", "write_file", {"path": "/tmp/f.txt"})

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()

    provider = MagicMock()
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    loop.approval_gate = gate  # inject our gate

    msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content=f"/approve {rec.approval_id}",
    )
    response = await loop._process_message(msg)
    assert response is not None
    assert "✅" in response.content
    assert "write_file" in response.content
    # LLM should NOT have been called
    provider.chat.assert_not_called()


async def test_agent_loop_deny_command_returns_confirmation(tmp_path: Path):
    from unittest.mock import AsyncMock, MagicMock

    from zen_claw.agent.loop import AgentLoop
    from zen_claw.bus.events import InboundMessage

    gate = _gate(tmp_path)
    rec = await gate.request_approval("cli:direct", "exec", {"cmd": "rm -rf /"})

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()

    provider = MagicMock()
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    loop.approval_gate = gate

    msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content=f"/deny {rec.approval_id}",
    )
    response = await loop._process_message(msg)
    assert response is not None
    assert "🚫" in response.content
    assert "exec" in response.content
    provider.chat.assert_not_called()


# ── tests: MEDIUM-009 — approval_required breaks the tool batch ───────────────


async def test_approval_required_stops_remaining_batch_tools(tmp_path):
    """
    When the first tool call in a batch triggers approval_required, the loop must
    NOT execute any subsequent tool calls in the same batch (MEDIUM-009).
    """
    from unittest.mock import AsyncMock, MagicMock

    from zen_claw.agent.loop import AgentLoop
    from zen_claw.bus.events import InboundMessage
    from zen_claw.providers.base import LLMResponse, ToolCallRequest

    gate = _gate(tmp_path)

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()

    # LLM returns two sensitive tool calls in one response batch.
    # After the first is blocked, the second must NOT be executed.
    exec_call = ToolCallRequest(id="tc-1", name="exec", arguments={"command": "rm -rf /"})
    write_call = ToolCallRequest(
        id="tc-2", name="write_file", arguments={"path": "/tmp/x", "content": "y"}
    )

    first_response = LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[exec_call, write_call],
    )
    # Second call (after the agent processes tool results) returns a plain text reply.
    second_response = LLMResponse(content="awaiting approval", finish_reason="stop", tool_calls=[])

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[first_response, second_response])

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    loop.approval_gate = gate
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    # Disable the plan phase so provider.chat mock responses are not consumed by it
    loop.execution.should_plan = lambda: False  # type: ignore[method-assign]

    # Track which tool names were actually executed via the registry
    executed_tools: list[str] = []
    original_execute = loop.tools.execute

    async def tracking_execute(name, *args, **kwargs):
        executed_tools.append(name)
        return await original_execute(name, *args, **kwargs)

    loop.tools.execute = tracking_execute  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content="please run rm -rf / and also write a file",
    )
    await loop._process_message(msg)

    # Neither exec nor write_file should have been executed — both are sensitive.
    # Critically, write_file must NOT have been attempted even though exec was blocked first.
    assert "exec" not in executed_tools, "exec should have been blocked by approval gate"
    assert "write_file" not in executed_tools, (
        "write_file must not run after exec was blocked (MEDIUM-009 batch-break)"
    )
    # Exactly one pending approval should exist (for exec, the first tool in the batch).
    pending = gate.list_pending()
    assert len(pending) == 1
    assert pending[0].tool_name == "exec"


# ── tests: CLI direct must see approval instructions ──────────────────────────


async def test_cli_sensitive_tool_returns_approval_instructions(tmp_path):
    """
    In direct CLI mode, approval instructions must be visible in the final response,
    not only sent to bus outbound.
    """
    from unittest.mock import AsyncMock, MagicMock

    from zen_claw.agent.loop import AgentLoop
    from zen_claw.bus.events import InboundMessage
    from zen_claw.providers.base import LLMResponse, ToolCallRequest

    gate = _gate(tmp_path)

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()

    exec_call = ToolCallRequest(id="tc-1", name="exec", arguments={"command": "ls"})
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(content="", finish_reason="tool_calls", tool_calls=[exec_call])
    )

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    loop.approval_gate = gate
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop.execution.should_plan = lambda: False  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        content="run ls",
    )
    response = await loop._process_message(msg)

    assert response is not None
    assert "/approve" in response.content
    assert "/deny" in response.content
    pending = gate.list_pending(session_id="cli:direct")
    assert len(pending) == 1
    assert pending[0].approval_id in response.content


# ── tests: MEDIUM-005 — bus notification failure is logged ────────────────────


async def test_notification_failure_is_logged_as_error(tmp_path: Path):
    """If bus.publish_outbound raises, logger.error must be called (MEDIUM-005)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    gate = _gate(tmp_path)

    bus = MagicMock()
    bus.publish_outbound = AsyncMock(side_effect=RuntimeError("bus down"))

    with patch("zen_claw.agent.approval_gate.logger") as mock_logger:
        rec = await gate.request_approval(
            "sess-1",
            "exec",
            {"cmd": "ls"},
            bus=bus,
            channel="telegram",
            chat_id="chat-99",
        )

    # The approval record must still be created despite the notification failure
    assert rec.status == ApprovalStatus.PENDING
    assert gate.get(rec.approval_id) is not None

    # logger.error must have been called with context info
    mock_logger.error.assert_called_once()
    call_args = " ".join(str(a) for a in mock_logger.error.call_args.args)
    assert "approval" in call_args.lower() or "notification" in call_args.lower()


async def test_notification_failure_does_not_raise(tmp_path: Path):
    """A bus error must not propagate out of request_approval (MEDIUM-005)."""
    from unittest.mock import AsyncMock, MagicMock

    gate = _gate(tmp_path)
    bus = MagicMock()
    bus.publish_outbound = AsyncMock(side_effect=ConnectionError("unreachable"))

    # Should complete without raising
    rec = await gate.request_approval("sess-1", "write_file", {"path": "/tmp/x"}, bus=bus)
    assert rec is not None


# ── tests: MEDIUM-007 — consume_approved encapsulates _pending access ─────────


async def test_consume_approved_returns_and_removes_approved_record(tmp_path: Path):
    """consume_approved() finds APPROVED record, removes it, returns it (MEDIUM-007)."""
    gate = _gate(tmp_path)
    args = {"cmd": "ls"}
    rec = await gate.request_approval("sess-1", "exec", args)
    gate.approve(rec.approval_id)

    consumed = gate.consume_approved("sess-1", "exec", args)

    assert consumed is not None
    assert consumed.approval_id == rec.approval_id
    # Must be removed from the gate — second call returns None
    assert gate.consume_approved("sess-1", "exec", args) is None


async def test_consume_approved_returns_none_for_pending_record(tmp_path: Path):
    """A record that is still PENDING must not be consumed."""
    gate = _gate(tmp_path)
    args = {"path": "/tmp/x"}
    await gate.request_approval("sess-1", "write_file", args)

    assert gate.consume_approved("sess-1", "write_file", args) is None


async def test_consume_approved_does_not_match_different_args(tmp_path: Path):
    """Args must match exactly; different args → no match."""
    gate = _gate(tmp_path)
    rec = await gate.request_approval("sess-1", "exec", {"cmd": "ls"})
    gate.approve(rec.approval_id)

    assert gate.consume_approved("sess-1", "exec", {"cmd": "rm -rf /"}) is None


async def test_consume_approved_does_not_match_different_session(tmp_path: Path):
    """Session ID must match exactly."""
    gate = _gate(tmp_path)
    args = {"cmd": "ls"}
    rec = await gate.request_approval("sess-A", "exec", args)
    gate.approve(rec.approval_id)

    assert gate.consume_approved("sess-B", "exec", args) is None


# ── tests: MEDIUM-006 — channel/chat_id from InboundMessage not messages[0] ──


async def test_approval_notification_uses_correct_channel_and_chat_id(tmp_path: Path):
    """request_approval must receive channel/chat_id from msg, not messages[0] (MEDIUM-006)."""
    from unittest.mock import AsyncMock, MagicMock

    from zen_claw.agent.loop import AgentLoop
    from zen_claw.bus.events import InboundMessage
    from zen_claw.providers.base import LLMResponse, ToolCallRequest

    gate = _gate(tmp_path)

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    # Capture outbound messages to verify the channel/chat_id
    outbound_msgs: list = []

    async def _capture_outbound(msg):
        outbound_msgs.append(msg)

    bus.publish_outbound = AsyncMock(side_effect=_capture_outbound)

    exec_call = ToolCallRequest(id="tc-1", name="exec", arguments={"command": "ls"})
    provider = MagicMock()
    provider.chat = AsyncMock(
        side_effect=[
            LLMResponse(content="", finish_reason="tool_calls", tool_calls=[exec_call]),
            LLMResponse(content="done", finish_reason="stop", tool_calls=[]),
        ]
    )

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    loop.approval_gate = gate
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop.execution.should_plan = lambda: False  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="telegram",
        chat_id="chat-42",
        sender_id="user",
        content="run ls",
    )
    await loop._process_message(msg)

    # The approval notification must have been sent to the correct channel/chat_id
    assert len(outbound_msgs) == 1
    assert outbound_msgs[0].channel == "telegram"
    assert outbound_msgs[0].chat_id == "chat-42"
