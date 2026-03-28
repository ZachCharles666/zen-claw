"""Tests for DeclarativeIntentEngine and daily assistant intents."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router_contracts import IntentToolContract
from zen_claw.agent.intent_router_daily import (
    CALENDAR_CHECK,
    DAILY_INTENTS,
    DAILY_WORKFLOW,
    EMAIL_CHECK,
    NOTION_QUERY,
    REMINDER,
)
from zen_claw.agent.intent_router_declarative import (
    DeclarativeIntent,
    DeclarativeIntentEngine,
    ParamRule,
)


# ── ParamRule tests ──────────────��─────────────────────────────────────────────


def test_param_rule_extract_string():
    rule = ParamRule(pattern=r"from\s+(\S+)", default=None)
    assert rule.extract("email from alice@test.com") == "alice@test.com"
    assert rule.extract("just a normal message") is None


def test_param_rule_extract_int():
    rule = ParamRule(pattern=r"(\d+)\s*封", default=5, type="int")
    assert rule.extract("显示10封邮件") == 10
    assert rule.extract("显示邮件") == 5


def test_param_rule_extract_bool():
    rule = ParamRule(pattern=r"(未读|unread)", default=True, type="bool")
    assert rule.extract("未读邮件") is True
    assert rule.extract("all email") is True  # default


def test_param_rule_no_pattern_returns_default():
    rule = ParamRule(default="google")
    assert rule.extract("anything here") == "google"


# ── DeclarativeIntent matching tests ────────────���──────────────────────────────


def test_email_check_matches_chinese():
    assert EMAIL_CHECK.match("未读邮件")
    assert EMAIL_CHECK.match("有新邮件吗")
    assert EMAIL_CHECK.match("查一下邮件")
    assert EMAIL_CHECK.match("收件箱")


def test_email_check_matches_english():
    assert EMAIL_CHECK.match("check my email")
    assert EMAIL_CHECK.match("show inbox")
    assert EMAIL_CHECK.match("unread emails")


def test_email_check_negative_patterns():
    """Send/reply/compose should NOT match email_check."""
    assert not EMAIL_CHECK.match("发邮件给 Alice")
    assert not EMAIL_CHECK.match("send email to bob@test.com")
    assert not EMAIL_CHECK.match("回复邮件")
    assert not EMAIL_CHECK.match("reply to the email")
    assert not EMAIL_CHECK.match("write an email")


def test_email_check_extract_params():
    params = EMAIL_CHECK.extract_params("显示10封未读邮件")
    assert params["limit"] == 10
    assert params["action"] == "read_inbox"


def test_calendar_check_matches():
    assert CALENDAR_CHECK.match("今天有什么会议")
    assert CALENDAR_CHECK.match("明天的日程")
    assert CALENDAR_CHECK.match("today's schedule")
    assert CALENDAR_CHECK.match("what's on my calendar")
    assert CALENDAR_CHECK.match("am I free tomorrow")


def test_calendar_check_negative_patterns():
    assert not CALENDAR_CHECK.match("创建一个会议")
    assert not CALENDAR_CHECK.match("schedule a meeting with Alice")
    assert not CALENDAR_CHECK.match("book a meeting room")


def test_reminder_matches():
    assert REMINDER.match("提醒我开会")
    assert REMINDER.match("15分钟后提醒我")
    assert REMINDER.match("remind me to call Alice")
    assert REMINDER.match("set a reminder for 3pm")


def test_notion_query_matches():
    assert NOTION_QUERY.match("查一下 Notion 里的项目进度")
    assert NOTION_QUERY.match("search notion for meeting notes")
    assert NOTION_QUERY.match("在 notion 上找一下设计文档")
    assert NOTION_QUERY.match("notion search API docs")


def test_notion_query_negative_patterns():
    assert not NOTION_QUERY.match("创建到 Notion")
    assert not NOTION_QUERY.match("write to notion")


def test_daily_workflow_matches():
    assert DAILY_WORKFLOW.match("做个早报")
    assert DAILY_WORKFLOW.match("来个今日摘要")
    assert DAILY_WORKFLOW.match("做个晚报")
    assert DAILY_WORKFLOW.match("morning briefing")
    assert DAILY_WORKFLOW.match("daily summary")
    assert DAILY_WORKFLOW.match("wrap-up")
    assert DAILY_WORKFLOW.match("做个周报")
    assert DAILY_WORKFLOW.match("weekly report")
    assert DAILY_WORKFLOW.match("本周的总结")


def test_no_false_positive_on_generic_text():
    """Generic messages should not match any daily intent."""
    generic = [
        "你好",
        "帮我写一首诗",
        "what is the meaning of life",
        "translate this to English",
        "2+2等于几",
    ]
    for text in generic:
        for intent in DAILY_INTENTS:
            assert not intent.match(text), f"'{text}' should not match {intent.name}"


# ── DeclarativeIntentEngine routing tests ────────��─────────────────────────────


def _make_tools_mock(tool_result_content: str = "ok", ok: bool = True) -> MagicMock:
    mock = MagicMock()
    result = MagicMock()
    result.ok = ok
    result.content = tool_result_content
    result.error = None if ok else MagicMock(message="test error", retryable=False)
    mock.execute = AsyncMock(return_value=result)
    return mock


@pytest.mark.asyncio
async def test_engine_routes_email_check():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    tools = _make_tools_mock("You have 3 unread emails.")

    result = await engine.route("未读邮件", tools=tools, trace_id="t1")

    assert result is not None
    assert result.handled
    assert result.route_status == "direct_success"
    assert result.intent_name == "email_check"
    assert "3 unread" in result.content
    tools.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_routes_tool_failure():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    tools = _make_tools_mock("", ok=False)

    result = await engine.route("check email", tools=tools, trace_id="t2")

    assert result is not None
    assert result.handled
    assert result.route_status == "direct_failed"
    assert result.recovery_outcome is not None


@pytest.mark.asyncio
async def test_engine_routes_hybrid_daily_workflow():
    engine = DeclarativeIntentEngine()
    engine.register(DAILY_WORKFLOW)
    tools = _make_tools_mock()

    result = await engine.route("做个早报", tools=tools, trace_id="t3")

    assert result is not None
    assert result.handled
    assert result.route_status == "needs_constrained_replan"
    assert result.intent_name == "daily_workflow"
    assert result.contract is not None
    assert result.contract.intent_mode == "hybrid"
    # Tool should NOT be called for hybrid intents
    tools.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_returns_none_on_no_match():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    tools = _make_tools_mock()

    result = await engine.route("你好世界", tools=tools, trace_id="t4")

    assert result is None


@pytest.mark.asyncio
async def test_engine_priority_first_match_wins():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    engine.register(NOTION_QUERY)
    tools = _make_tools_mock("inbox results")

    # "check email" should match EMAIL_CHECK first, not NOTION_QUERY
    result = await engine.route("check my email", tools=tools, trace_id="t5")

    assert result is not None
    assert result.intent_name == "email_check"


@pytest.mark.asyncio
async def test_full_engine_with_all_daily_intents():
    engine = DeclarativeIntentEngine()
    engine.register_many(DAILY_INTENTS)
    tools = _make_tools_mock("result")

    cases = [
        ("未读邮件", "email_check", "direct_success"),
        ("今天有什么会议", "calendar_check", "direct_success"),
        ("提醒我3点开会", "reminder", "direct_success"),
        ("查一下 Notion 里的进度", "notion_query", "direct_success"),
        ("做个早报", "daily_workflow", "needs_constrained_replan"),
    ]
    for text, expected_name, expected_status in cases:
        result = await engine.route(text, tools=tools, trace_id="test")
        assert result is not None, f"'{text}' should match"
        assert result.intent_name == expected_name, f"'{text}' should match {expected_name}, got {result.intent_name}"
        assert result.route_status == expected_status, f"'{text}' status should be {expected_status}"


# ── IntentRouter integration test ────���─────────────────────────────────────────


@pytest.mark.asyncio
async def test_intent_router_declarative_before_native():
    """Declarative intents should be checked before native mixin handlers."""
    from zen_claw.agent.intent_router import IntentRouter

    router = IntentRouter(allow_runtime_constrained_replan=True)
    tools = _make_tools_mock("5 unread emails")

    # "未读邮件" should be caught by declarative engine
    result = await router.route("未读邮件", tools=tools, trace_id="t6")
    assert result.handled
    assert result.intent_name == "email_check"
    assert "declarative" in (result.diagnostic or "")


@pytest.mark.asyncio
async def test_intent_router_falls_through_to_native():
    """Unmatched declarative should fall through to native mixin handlers."""
    from zen_claw.agent.intent_router import IntentRouter

    router = IntentRouter(allow_runtime_constrained_replan=True)
    tools = _make_tools_mock("result")

    # "北京天气" should NOT match declarative, but should match native weather handler
    result = await router.route("北京天气", tools=tools, trace_id="t7")
    # Should be handled by native weather handler (or miss if API not available)
    # The key test is that it didn't match declarative "email_check" etc.
    if result.handled:
        assert result.intent_name in {"weather", None}


@pytest.mark.asyncio
async def test_intent_router_misses_generic_text():
    """Generic text should not match any intent."""
    from zen_claw.agent.intent_router import IntentRouter

    router = IntentRouter(allow_runtime_constrained_replan=True)
    tools = _make_tools_mock()

    result = await router.route("帮我写一首诗", tools=tools, trace_id="t8")
    # Should either be unhandled or not match daily intents
    if result.handled:
        assert result.intent_name not in {"email_check", "calendar_check", "reminder", "notion_query"}
