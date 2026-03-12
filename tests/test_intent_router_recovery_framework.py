from zen_claw.agent.intent_router import (
    IntentRouter,
    RecoveryBlocker,
    RecoveryGuidance,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryStrategy,
)


def test_recovery_guidance_from_plan_flattens_structured_fields() -> None:
    plan = RecoveryPlan(
        blocker=RecoveryBlocker(
            kind="input_ambiguous",
            description="时区映射无法确认",
            missing_requirement="可确认的城市或标准时区名",
        ),
        strategies=[
            RecoveryStrategy(kind="local_correction", detail="重试更明确的时区输入"),
            RecoveryStrategy(kind="guidance_only", detail="提供替代说明"),
        ],
        checked_scope=["已按城市别名解析"],
        next_steps=["你可以提供标准时区名"],
        fallback_options=["也可以先返回当前时区时间"],
    )

    guidance = RecoveryGuidance.from_plan(plan)

    assert guidance.blocker == "时区映射无法确认"
    assert guidance.missing_requirement == "可确认的城市或标准时区名"
    assert guidance.checked_scope == ["已按城市别名解析"]
    assert guidance.next_steps == ["你可以提供标准时区名"]
    assert guidance.fallback_options == ["也可以先返回当前时区时间"]


def test_build_recovery_guidance_from_plan_keeps_existing_message_shape() -> None:
    plan = RecoveryPlan(
        blocker=RecoveryBlocker(
            kind="source_scope_insufficient",
            description="内置天气源的时间范围上限",
            missing_requirement="超过16天的可信长周期天气数据",
        ),
        strategies=[
            RecoveryStrategy(kind="semantic_reroute", detail="优先改走历史天气路径"),
            RecoveryStrategy(kind="guidance_only", detail="提供替代方案"),
        ],
        checked_scope=["已检查主天气源", "已检查备用天气源"],
        next_steps=["现在可以先返回最近16天的真实天气"],
        fallback_options=["也可以给一份标注为估算的趋势版"],
    )

    message = IntentRouter._build_recovery_guidance_from_plan(
        summary="暂时无法直接提供未来70天的天气。",
        plan=plan,
    )

    assert "暂时无法直接提供未来70天的天气。" in message
    assert "当前卡点不是权限或审批问题，而是内置天气源的时间范围上限" in message
    assert "缺的是超过16天的可信长周期天气数据" in message
    assert "我已经先检查了：我先检查了当前可用天气数据的范围。" in message
    assert "下一步可继续这样处理：现在可以先返回最近16天的真实天气。" in message
    assert "如果你接受替代方案，我也可以这样继续：也可以给一份标注为估算的趋势版。" in message


def test_build_recovery_guidance_message_hides_internal_source_details() -> None:
    guidance = RecoveryGuidance(
        blocker="维基百科上游站点未返回可用摘要",
        missing_requirement="可访问且返回有效摘要的站点内容",
        checked_scope=[
            "当前直达维基百科路由已尝试主站点摘要接口",
            "当前直达维基百科路由已尝试备用语言站点与 query API",
        ],
        next_steps=["你可以换一个更明确的词条名，我继续帮你重试"],
        fallback_options=["如果你愿意，也可以改成更具体的问题"],
    )

    message = IntentRouter._build_recovery_guidance_message(
        summary="暂时无法从维基百科获取摘要。",
        guidance=guidance,
    )

    assert "主站点摘要接口" not in message
    assert "query API" not in message
    assert "我先尝试了当前可用的百科摘要来源和词条匹配方式" in message


def test_weather_days_limit_outcome_returns_structured_recovery_outcome() -> None:
    outcome = IntentRouter._build_weather_days_limit_outcome(
        "成都",
        requested_days=70,
        max_supported_days=16,
        request_scope="future",
    )

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.mode == "guided"
    assert outcome.plan is not None
    assert outcome.plan.blocker.kind == "source_scope_insufficient"
    assert "暂时无法直接提供成都未来70天的天气" in outcome.content


def test_fixed_site_failure_outcome_returns_structured_recovery_outcome() -> None:
    outcome = IntentRouter._build_fixed_site_failure_outcome(site="wikipedia", topic="Alan Turing")

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.mode == "guided"
    assert outcome.plan is not None
    assert outcome.plan.blocker.kind == "upstream_unavailable"
    assert "暂时无法从维基百科获取“Alan Turing”的摘要" in outcome.content


def test_timezone_fallback_resolution_plan_can_back_a_resolved_outcome() -> None:
    content = "纽约当前时间：2026-03-08 06:00:00 EDT"
    outcome = IntentRouter._resolved_outcome(
        content=content,
        plan=IntentRouter._build_timezone_fallback_resolution_plan(),
    )

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.mode == "resolved"
    assert outcome.plan is not None
    assert outcome.plan.blocker.kind == "environment_missing"
    assert outcome.content == content


def test_weather_history_resolution_plan_can_back_a_resolved_outcome() -> None:
    content = "成都最近70天天气记录：\n2026-01-29 大部晴朗 10~18°C"
    outcome = IntentRouter._resolved_outcome(
        content=content,
        plan=IntentRouter._build_weather_history_resolution_plan(days=70),
    )

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.mode == "resolved"
    assert outcome.plan is not None
    assert outcome.plan.blocker.kind == "source_scope_insufficient"
    assert "天气记录" in outcome.content
