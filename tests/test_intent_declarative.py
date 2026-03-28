"""Tests for DeclarativeIntentEngine and daily assistant intents."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router_contracts import ControlSignals, IntentToolContract
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


def test_param_rule_extract_with_trace_returns_consumed_span():
    rule = ParamRule(pattern=r"from\s+(\S+)", default=None)
    value, ranges = rule.extract_with_trace("email from alice@test.com")
    assert value == "alice@test.com"
    assert ranges == [(11, 25)]


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
    assert result.route_candidate is not None
    assert result.route_candidate["match"] == "email_check"
    assert result.route_candidate["source"] == "declarative"
    assert result.route_candidate["raw_confidence"] == 0.9
    assert result.safety_valve_outcome == "execute"
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
    assert result.route_candidate is not None
    assert result.route_candidate["match"] == "email_check"
    assert result.route_candidate["source"] == "declarative"
    assert result.route_candidate["raw_confidence"] == 0.9
    assert result.safety_valve_outcome == "execute"


@pytest.mark.asyncio
async def test_engine_delegates_direct_intent_on_low_history_confidence():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    tools = _make_tools_mock("You have 3 unread emails.")

    result = await engine.route(
        "未读邮件",
        tools=tools,
        trace_id="t2b",
        control_signals=ControlSignals(history_confidence={"email_check": 0.5}),
    )

    assert result is not None
    assert result.handled
    assert result.route_status == "needs_constrained_replan"
    assert result.intent_name == "email_check"
    assert result.content == "未读邮件"
    assert result.delegate_reason == "safety_valve_low_confidence"
    assert result.safety_valve_outcome == "delegate"
    assert result.safety_valve_trace is not None
    assert result.safety_valve_trace["weighted_confidence"] == 0.45
    assert result.route_candidate is not None
    assert result.route_candidate["match"] == "email_check"
    tools.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_delegates_when_param_residual_ratio_is_high():
    intent = DeclarativeIntent(
        name="mail_from",
        patterns=[r"email"],
        params={"sender": ParamRule(pattern=r"from\s+(\S+)", default=None)},
        tool_name="email",
        contract=IntentToolContract(
            intent_name="mail_from",
            preferred_tools=["email"],
            allowed_tools={"email"},
            denied_tools={"exec"},
        ),
    )
    engine = DeclarativeIntentEngine()
    engine.register(intent)
    tools = _make_tools_mock("ok")

    result = await engine.route(
        "check email from alice about budget and summarize the risk",
        tools=tools,
        trace_id="t2c",
        control_signals=ControlSignals(history_confidence={"mail_from": 1.0}),
    )

    assert result is not None
    assert result.route_status == "needs_constrained_replan"
    assert result.delegate_reason == "safety_valve_high_residual_ratio"
    assert result.safety_valve_trace is not None
    assert result.safety_valve_trace["residual_ratio"] > 0.45
    tools.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_delegates_when_multiple_candidates_match():
    shared_contract = IntentToolContract(
        intent_name="shared",
        preferred_tools=["email"],
        allowed_tools={"email"},
        denied_tools={"exec"},
    )
    engine = DeclarativeIntentEngine()
    engine.register(
        DeclarativeIntent(name="alpha", patterns=[r"check"], tool_name="email", contract=shared_contract)
    )
    engine.register(
        DeclarativeIntent(name="beta", patterns=[r"email"], tool_name="email", contract=shared_contract)
    )
    tools = _make_tools_mock("ok")

    result = await engine.route(
        "check email",
        tools=tools,
        trace_id="t2d",
        control_signals=ControlSignals(history_confidence={"alpha": 1.0, "beta": 1.0}),
    )

    assert result is not None
    assert result.delegate_reason == "safety_valve_multiple_candidates"
    assert result.safety_valve_trace is not None
    assert result.safety_valve_trace["matched_candidates_count"] == 2
    tools.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_delegates_when_context_pronoun_present():
    engine = DeclarativeIntentEngine()
    engine.register(EMAIL_CHECK)
    tools = _make_tools_mock("ok")

    result = await engine.route(
        "查一下那个邮件",
        tools=tools,
        trace_id="t2e",
        control_signals=ControlSignals(history_confidence={"email_check": 1.0}),
    )

    assert result is not None
    assert result.delegate_reason == "safety_valve_context_pronoun"
    assert result.safety_valve_trace is not None
    assert result.safety_valve_trace["has_context_pronoun"] is True
    tools.execute.assert_not_awaited()


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
    assert result.route_candidate is not None
    assert result.route_candidate["match"] == "daily_workflow"
    assert result.route_candidate["source"] == "declarative"
    assert result.route_candidate["raw_confidence"] == 0.75
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
    assert result.route_candidate is not None
    assert result.route_candidate["source"] == "declarative"


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


@pytest.mark.asyncio
async def test_intent_router_correction_hard_rule_delegates_before_rules():
    from zen_claw.agent.intent_router import IntentRouter

    router = IntentRouter(allow_runtime_constrained_replan=True)
    router.set_control_context(
        {
            "intent_router_last_rule": {
                "intent_name": "email_check",
                "source": "declarative",
                "route_status": "direct_success",
            }
        }
    )
    tools = _make_tools_mock("should not run")

    result = await router.route("不是这个意思", tools=tools, trace_id="t9")

    assert result.handled
    assert result.route_status == "needs_constrained_replan"
    assert result.delegate_reason == "correction_override"
    assert result.safety_valve_outcome == "delegate"
    assert result.diagnostic == "gate1:correction_override"
    tools.execute.assert_not_awaited()
