"""Regression tests for quick_note intent: parser + handler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router import IntentRouter


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tools(ok: bool = True) -> MagicMock:
    mock = MagicMock()
    result = MagicMock()
    result.ok = ok
    result.content = "ok"
    result.error = None if ok else MagicMock(message="write failed", retryable=False)
    mock.execute = AsyncMock(return_value=result)
    return mock


def _router() -> IntentRouter:
    return IntentRouter()


# ── parser: Chinese capture patterns ─────────────────────────────────────────


def test_parser_matches_jiyixia() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("记一下明天开会")
    assert parsed is not None
    assert "明天开会" in parsed["note_content"]


def test_parser_matches_jixialai() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("帮我记下来：买牛奶")
    assert parsed is not None
    assert "买牛奶" in parsed["note_content"]


def test_parser_matches_jiluyixia() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("记录一下 周五要交报告")
    assert parsed is not None
    assert "周五要交报告" in parsed["note_content"]


def test_parser_matches_beiwang() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("加个备忘：下周一要开周会")
    assert parsed is not None
    assert "下周一要开周会" in parsed["note_content"]


def test_parser_matches_jiizhu() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("记住 这个密码是1234")
    assert parsed is not None
    assert "这个密码是1234" in parsed["note_content"]


# ── parser: English capture patterns ─────────────────────────────────────────


def test_parser_matches_note_colon() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("note: buy groceries after work")
    assert parsed is not None
    assert "buy groceries after work" in parsed["note_content"]


def test_parser_matches_jot_down() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("jot down call Alice tomorrow")
    assert parsed is not None
    assert "call Alice tomorrow" in parsed["note_content"]


def test_parser_matches_remember() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("remember this: meeting at 3pm")
    assert parsed is not None
    assert "meeting at 3pm" in parsed["note_content"]


def test_parser_matches_quick_note_colon() -> None:
    r = _router()
    parsed = r._extract_quick_note_request("quick note: review PR before EOD")
    assert parsed is not None
    assert "review PR before EOD" in parsed["note_content"]


# ── parser: rejection cases ───────────────────────────────────────────────────


def test_parser_rejects_search_in_notes() -> None:
    r = _router()
    assert r._extract_quick_note_request("在笔记里搜索开会记录") is None


def test_parser_rejects_find_notes() -> None:
    r = _router()
    assert r._extract_quick_note_request("find meeting notes from last week") is None


def test_parser_returns_none_for_empty_input() -> None:
    r = _router()
    assert r._extract_quick_note_request("") is None


def test_parser_returns_none_for_short_content() -> None:
    r = _router()
    # note content must be >= 2 chars
    assert r._extract_quick_note_request("记一下a") is None


# ── handler: success path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_write_success_returns_direct_success() -> None:
    tools = _make_tools(ok=True)
    r = _router()
    result = await r.route("记一下采购清单：牛奶面包鸡蛋", tools=tools, trace_id="t-note")

    assert result.handled is True
    assert result.intent_name == "quick_note"
    assert result.route_status == "direct_success"
    assert "牛奶面包鸡蛋" in result.content


@pytest.mark.asyncio
async def test_handler_passes_write_file_tool_call() -> None:
    tools = _make_tools(ok=True)
    r = _router()
    await r.route("记一下采购列表：牛奶、鸡蛋", tools=tools, trace_id="t-note-call")

    tools.execute.assert_called_once()
    call_args = tools.execute.call_args
    assert call_args[0][0] == "write_file"
    payload = call_args[0][1]
    assert payload.get("mode") == "append"
    assert "牛奶" in payload.get("content", "")


@pytest.mark.asyncio
async def test_handler_note_file_uses_date_filename() -> None:
    tools = _make_tools(ok=True)
    r = _router()
    await r.route("记一下开会", tools=tools, trace_id="t-note-date")

    call_args = tools.execute.call_args
    payload = call_args[0][1]
    note_path = payload.get("path", "")
    # Should end with YYYY-MM-DD.md
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}\.md$", note_path), (
        f"Expected date-based filename, got: {note_path}"
    )


@pytest.mark.asyncio
async def test_handler_note_entry_includes_timestamp_bracket() -> None:
    tools = _make_tools(ok=True)
    r = _router()
    await r.route("记一下下班前提交代码", tools=tools, trace_id="t-note-ts")

    call_args = tools.execute.call_args
    payload = call_args[0][1]
    content = payload.get("content", "")
    # Entry should include [HH:MM] timestamp pattern
    import re
    assert re.search(r"\[\d{2}:\d{2}\]", content), (
        f"Expected [HH:MM] timestamp in entry, got: {content!r}"
    )


# ── handler: failure path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_write_failure_returns_direct_failed() -> None:
    tools = _make_tools(ok=False)
    r = _router()
    result = await r.route("记一下买牛奶和面包", tools=tools, trace_id="t-note-fail")

    assert result.handled is True
    assert result.intent_name == "quick_note"
    assert result.route_status == "direct_failed"


# ── _resolve_quick_note_dir default ──────────────────────────────────────────


def test_resolve_quick_note_dir_default_is_notes_inbox() -> None:
    r = _router()
    path = r._resolve_quick_note_dir()
    assert path == Path("~/notes/inbox").expanduser()
