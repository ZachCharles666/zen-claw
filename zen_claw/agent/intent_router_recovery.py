"""Recovery/result builders for the intent router."""

from __future__ import annotations

from typing import Literal

from zen_claw.agent.intent_router_contracts import (
    IntentRouteResult,
    IntentToolContract,
    RecoveryBlocker,
    RecoveryGuidance,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryStrategy,
)


class IntentRouterRecoveryMixin:
    @staticmethod
    def _direct_failed(
        *,
        intent_name: str,
        content: str,
        contract: IntentToolContract,
        diagnostic: str,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent_name,
            content=content,
            contract=contract,
            route_status="direct_failed",
            diagnostic=diagnostic,
            recovery_outcome=recovery_outcome,
        )

    @staticmethod
    def _direct_success(
        *,
        intent_name: str,
        content: str,
        contract: IntentToolContract,
        diagnostic: str | None = None,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent_name,
            content=content,
            contract=contract,
            route_status="direct_success",
            diagnostic=diagnostic,
            recovery_outcome=recovery_outcome,
        )

    @classmethod
    def _recovery_outcome_from_plan(
        cls,
        *,
        summary: str,
        plan: RecoveryPlan,
        mode: Literal["guided", "failed"] = "guided",
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            mode=mode,
            content=cls._build_recovery_guidance_from_plan(summary=summary, plan=plan),
            plan=plan,
        )

    @classmethod
    def _direct_failed_with_plan(
        cls,
        *,
        intent_name: str,
        summary: str,
        plan: RecoveryPlan,
        contract: IntentToolContract,
        diagnostic: str,
        mode: Literal["guided", "failed"] = "guided",
    ) -> IntentRouteResult:
        outcome = cls._recovery_outcome_from_plan(summary=summary, plan=plan, mode=mode)
        return cls._direct_failed(
            intent_name=intent_name,
            content=outcome.content,
            contract=contract,
            diagnostic=diagnostic,
            recovery_outcome=outcome,
        )

    @staticmethod
    def _resolved_outcome(*, content: str, plan: RecoveryPlan) -> RecoveryOutcome:
        return RecoveryOutcome(mode="resolved", content=content, plan=plan)

    @classmethod
    def _direct_success_with_plan(
        cls,
        *,
        intent_name: str,
        content: str,
        plan: RecoveryPlan,
        contract: IntentToolContract,
        diagnostic: str,
    ) -> IntentRouteResult:
        return cls._direct_success(
            intent_name=intent_name,
            content=content,
            contract=contract,
            diagnostic=diagnostic,
            recovery_outcome=cls._resolved_outcome(content=content, plan=plan),
        )

    @classmethod
    def _direct_failed_with_outcome(
        cls,
        *,
        intent_name: str,
        outcome: RecoveryOutcome,
        contract: IntentToolContract,
        diagnostic: str,
    ) -> IntentRouteResult:
        return cls._direct_failed(
            intent_name=intent_name,
            content=outcome.content,
            contract=contract,
            diagnostic=diagnostic,
            recovery_outcome=outcome,
        )

    @staticmethod
    def _needs_constrained_replan(
        *,
        intent_name: str,
        contract: IntentToolContract,
        diagnostic: str,
        content: str | None = None,
        skip_planning: bool = False,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent_name,
            content=content,
            contract=contract,
            route_status="needs_constrained_replan",
            diagnostic=diagnostic,
            skip_planning=skip_planning,
            recovery_outcome=recovery_outcome,
        )

    @staticmethod
    def _needs_explicit_approval(
        *,
        intent_name: str,
        contract: IntentToolContract,
        diagnostic: str,
        content: str | None = None,
        skip_planning: bool = False,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent_name,
            content=content,
            contract=contract,
            route_status="needs_explicit_approval",
            diagnostic=diagnostic,
            skip_planning=skip_planning,
            recovery_outcome=recovery_outcome,
        )

    @classmethod
    def _needs_constrained_replan_with_outcome(
        cls,
        *,
        intent_name: str,
        outcome: RecoveryOutcome,
        contract: IntentToolContract,
        diagnostic: str,
        content: str | None = None,
        skip_planning: bool = False,
    ) -> IntentRouteResult:
        return cls._needs_constrained_replan(
            intent_name=intent_name,
            contract=contract,
            diagnostic=diagnostic,
            content=content or outcome.content,
            skip_planning=skip_planning,
            recovery_outcome=outcome,
        )

    @classmethod
    def _needs_constrained_replan_with_plan(
        cls,
        *,
        intent_name: str,
        summary: str,
        plan: RecoveryPlan,
        contract: IntentToolContract,
        diagnostic: str,
        content: str | None = None,
        skip_planning: bool = False,
        mode: Literal["guided", "failed"] = "guided",
    ) -> IntentRouteResult:
        outcome = cls._recovery_outcome_from_plan(summary=summary, plan=plan, mode=mode)
        return cls._needs_constrained_replan(
            intent_name=intent_name,
            contract=contract,
            diagnostic=diagnostic,
            content=content or outcome.content,
            skip_planning=skip_planning,
            recovery_outcome=outcome,
        )

    @classmethod
    def _needs_explicit_approval_with_plan(
        cls,
        *,
        intent_name: str,
        summary: str,
        plan: RecoveryPlan,
        contract: IntentToolContract,
        diagnostic: str,
        content: str | None = None,
        skip_planning: bool = False,
        mode: Literal["guided", "failed"] = "guided",
    ) -> IntentRouteResult:
        outcome = cls._recovery_outcome_from_plan(summary=summary, plan=plan, mode=mode)
        return cls._needs_explicit_approval(
            intent_name=intent_name,
            contract=contract,
            diagnostic=diagnostic,
            content=content or outcome.content,
            skip_planning=skip_planning,
            recovery_outcome=outcome,
        )

    @staticmethod
    def _build_timezone_fallback_resolution_plan() -> RecoveryPlan:
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="environment_missing",
                description="本地标准时区数据暂不可用",
                missing_requirement="可用的 IANA 时区数据或 tzdata",
            ),
            strategies=[
                RecoveryStrategy(
                    kind="local_correction",
                    detail="先按内置固定时区偏移完成当前时间计算",
                ),
            ],
            checked_scope=[
                "当前直达时间路由已尝试按标准时区数据解析",
                "当前直达时间路由已尝试按内置固定时区偏移兜底",
            ],
            next_steps=[
                "如果后续补齐标准时区数据，时间结果会优先回到完整时区规则计算",
            ],
            fallback_options=[],
        )

    @staticmethod
    def _build_timezone_fuzzy_alias_resolution_plan() -> RecoveryPlan:
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="locally_correctable",
                description="输入里的城市别名存在轻微拼写偏差",
                missing_requirement="可映射到已知时区的近似城市别名",
            ),
            strategies=[
                RecoveryStrategy(
                    kind="local_correction",
                    detail="先按低风险 fuzzy alias lookup 纠正城市别名后继续时区解析",
                ),
            ],
            checked_scope=[
                "当前直达时间路由已尝试精确城市别名匹配",
                "当前直达时间路由已尝试低风险 fuzzy alias lookup",
            ],
            next_steps=[
                "如果你继续用相近写法查询，我会优先按这条低风险纠错路径继续处理",
            ],
            fallback_options=[],
        )

    @staticmethod
    def _build_weather_history_resolution_plan(*, days: int) -> RecoveryPlan:
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="source_scope_insufficient",
                description="未来天气预报范围不足以覆盖最近长跨度请求",
                missing_requirement="可用的历史天气数据路径",
            ),
            strategies=[
                RecoveryStrategy(
                    kind="semantic_reroute",
                    detail="将最近/过去的长跨度天气请求改走历史 archive 路径",
                ),
            ],
            checked_scope=[
                "当前直达天气路由已识别该请求更适合走历史天气路径",
                "当前直达天气路由已尝试历史天气 archive 数据",
            ],
            next_steps=[
                f"如果你需要，我也可以继续按同样方式补充最近{days}天内其他城市的历史天气",
            ],
            fallback_options=[],
        )

    @staticmethod
    def _build_weather_fallback_source_resolution_plan(*, location: str, days: int) -> RecoveryPlan:
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description="首选天气源未稳定返回完整天气结果",
                missing_requirement="至少一个可访问且返回有效天气结果的备用来源",
            ),
            strategies=[
                RecoveryStrategy(
                    kind="fallback_source",
                    detail="主天气源未返回可用结果时改走 Open-Meteo 备用路径",
                ),
            ],
            checked_scope=[
                "当前直达天气路由已尝试首选天气源",
                "当前直达天气路由已尝试 Open-Meteo 备用天气源",
            ],
            next_steps=[
                f"如果你继续查询 {location} 未来{days}天内天气，我会优先复用这条已成功的备用路径",
            ],
            fallback_options=[],
        )

    @staticmethod
    def _build_weather_source_failure_plan(*, location: str) -> RecoveryPlan:
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description="当前可用天气来源暂时没有返回稳定结果",
                missing_requirement="至少一个可访问且返回有效天气结果的数据来源",
            ),
            strategies=[
                RecoveryStrategy(kind="fallback_source", detail="继续尝试其他可用天气来源"),
                RecoveryStrategy(kind="guidance_only", detail="在当前来源都失败时提示稍后重试或缩短时间范围"),
            ],
            checked_scope=[
                "当前直达天气路由已尝试多个可用天气来源",
                "当前直达天气路由已检查返回内容是否能生成有效天气结果",
            ],
            next_steps=[
                f"你可以稍后重试，我会重新检查{location}的天气数据",
                "如果你只需要更短时间范围，我也可以先尝试缩短查询范围",
            ],
            fallback_options=[
                "如果你愿意，也可以先改问更短时间范围，或只查询今天/未来几天的天气",
            ],
        )

    @classmethod
    def _build_exchange_failure_outcome(cls, source: str, target: str) -> RecoveryOutcome:
        plan = RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description="汇率上游服务未返回可用结果",
                missing_requirement="至少一个可访问且返回目标货币对的汇率源",
            ),
            strategies=[
                RecoveryStrategy(kind="fallback_source", detail="继续尝试备用汇率源"),
                RecoveryStrategy(
                    kind="reverse_solve",
                    detail="当正向货币对缺失时尝试反向货币对后求倒数",
                ),
                RecoveryStrategy(kind="guidance_only", detail="在上游均失败时建议稍后重试"),
            ],
            checked_scope=[
                "当前直达汇率路由已尝试主汇率源",
                "当前直达汇率路由已尝试备用汇率源与反向货币对求解",
            ],
            next_steps=[
                "你可以稍后重试，我会再次检查主汇率源和备用汇率源",
            ],
            fallback_options=[
                "如果你只需要大致换算，我也可以按最近常见区间先给你一个明确标注为估算的近似值",
            ],
        )
        return cls._recovery_outcome_from_plan(
            summary=(
                f"暂时无法获取{source}->{target}的汇率数据。"
                "主汇率源和备用汇率源都未成功响应。"
            ),
            plan=plan,
        )

    @staticmethod
    def _build_exchange_resolution_plan(
        *, source: str, target: str, recovery_kind: Literal["fallback_source", "reverse_solve"]
    ) -> RecoveryPlan:
        checked_scope = [
            "当前直达汇率路由已尝试主汇率源",
            "当前直达汇率路由已尝试备用汇率源",
        ]
        strategy_detail = "主汇率源失败后改走备用汇率源"
        if recovery_kind == "reverse_solve":
            checked_scope.append("当前直达汇率路由已尝试反向货币对求解")
            strategy_detail = "备用汇率源缺少正向货币对时改走反向货币对后求倒数"
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description="主汇率获取路径未直接返回目标货币对",
                missing_requirement="至少一条可用的备用汇率或可逆推货币对路径",
            ),
            strategies=[RecoveryStrategy(kind=recovery_kind, detail=strategy_detail)],
            checked_scope=checked_scope,
            next_steps=[
                f"如果你继续查 {source}->{target}，我会优先沿着这条已成功的扩展路径继续获取",
            ],
            fallback_options=[],
        )

    @staticmethod
    def _build_fixed_site_resolution_plan(
        *, site: str, recovery_kind: Literal["fallback_source", "same_site_search"]
    ) -> RecoveryPlan:
        site_label = "维基百科" if site == "wikipedia" else site
        strategy_detail = "主摘要接口未直接返回结果时改走备用语言站点或 query API"
        if recovery_kind == "same_site_search":
            strategy_detail = "词条名不精确时先站内搜索，再重试摘要抓取"
        return RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description=f"{site_label}直达摘要路径未直接返回可用内容",
                missing_requirement="可用的同站备用抓取路径或更准确的词条定位",
            ),
            strategies=[RecoveryStrategy(kind=recovery_kind, detail=strategy_detail)],
            checked_scope=[
                f"当前直达{site_label}路由已先尝试首选摘要路径",
                f"当前直达{site_label}路由已按需扩展到同站备用抓取或词条搜索路径",
            ],
            next_steps=[
                "如果你继续追问同一词条，我会优先复用这条已验证可行的扩展路径",
            ],
            fallback_options=[],
        )

    @classmethod
    def _build_weather_days_limit_outcome(
        cls,
        location: str,
        *,
        requested_days: int,
        max_supported_days: int,
        request_scope: Literal["recent", "future"] = "recent",
    ) -> RecoveryOutcome:
        scope_label = "未来" if request_scope == "future" else "最近"
        plan = RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="source_scope_insufficient",
                description="内置天气源的时间范围上限",
                missing_requirement=f"超过{max_supported_days}天的可信长周期天气数据",
            ),
            strategies=[
                RecoveryStrategy(
                    kind="semantic_reroute",
                    detail="对明显的最近/过去 N 天请求优先改走历史天气路径",
                ),
                RecoveryStrategy(
                    kind="guidance_only",
                    detail="在无法继续扩展时给出更短周期或估算趋势替代方案",
                ),
            ],
            checked_scope=[
                "当前直达天气路由已评估主天气源的覆盖范围",
                "当前直达天气路由已评估备用天气源的覆盖范围",
            ],
            next_steps=[
                f"我现在可以先返回{location}最近{max_supported_days}天的真实天气",
                "如果后续补上更长周期的可信天气源，这一步应优先继续扩展求解",
            ],
            fallback_options=[
                f"我也可以继续按季节趋势补一份标注为估算的{requested_days}天天气趋势版",
                "如果你只需要更短时间范围，也可以直接改问 16 天以内的天气",
            ],
        )
        return cls._recovery_outcome_from_plan(
            summary=(
                f"当前内置天气数据源最多支持未来{max_supported_days}天天气预报，"
                f"暂时无法直接提供{location}{scope_label}{requested_days}天的天气。"
            ),
            plan=plan,
        )

    @staticmethod
    def _build_fixed_site_failure_outcome(*, site: str, topic: str) -> RecoveryOutcome:
        site_label = "维基百科" if site == "wikipedia" else site
        plan = RecoveryPlan(
            blocker=RecoveryBlocker(
                kind="upstream_unavailable",
                description=f"{site_label}上游站点未返回可用摘要",
                missing_requirement="可访问且返回有效摘要的站点内容",
            ),
            strategies=[
                RecoveryStrategy(kind="fallback_source", detail="继续尝试不同语言站点与 query API 备用链路"),
                RecoveryStrategy(kind="same_site_search", detail="词条不精确时先站内搜索，再重新抓取摘要"),
                RecoveryStrategy(kind="guidance_only", detail="在上游仍不可用时提示更明确词条名或稍后重试"),
            ],
            checked_scope=[
                f"当前直达{site_label}路由已尝试主站点摘要接口",
                f"当前直达{site_label}路由已尝试备用语言站点与 query API",
            ],
            next_steps=[
                "你可以换一个更明确的词条名，我继续帮你重试",
                "如果只是站点临时异常，稍后再试通常就能恢复",
            ],
            fallback_options=[
                "如果你愿意，也可以改成更具体的问题，我先基于已知常识给你一个简述方向",
            ],
        )
        return IntentRouterRecoveryMixin._recovery_outcome_from_plan(
            summary=(
                f"暂时无法从{site_label}获取“{topic}”的摘要。"
                "主站点和备用站点都未成功返回可用内容。"
            ),
            plan=plan,
        )

    @classmethod
    def _build_recovery_guidance_from_plan(cls, *, summary: str, plan: RecoveryPlan) -> str:
        return cls._build_recovery_guidance_message(
            summary=summary,
            guidance=RecoveryGuidance.from_plan(plan),
        )

    @staticmethod
    def _build_recovery_guidance_message(*, summary: str, guidance: RecoveryGuidance) -> str:
        parts = [summary.strip()]
        checked = IntentRouterRecoveryMixin._humanize_checked_scope(guidance.checked_scope)
        next_steps = "；".join(item.strip("；。 ") for item in guidance.next_steps if item.strip())
        fallbacks = "；".join(item.strip("；。 ") for item in guidance.fallback_options if item.strip())
        parts.append(
            f"当前卡点不是权限或审批问题，而是{guidance.blocker}，缺的是{guidance.missing_requirement}。"
        )
        if checked:
            parts.append(f"我已经先检查了：{checked}。")
        if next_steps:
            parts.append(f"下一步可继续这样处理：{next_steps}。")
        if fallbacks:
            parts.append(f"如果你接受替代方案，我也可以这样继续：{fallbacks}。")
        return "".join(parts)

    @staticmethod
    def _humanize_checked_scope(items: list[str]) -> str:
        seen: set[str] = set()
        simplified: list[str] = []
        for raw in items:
            item = raw.strip("；。 ")
            if not item:
                continue
            lower = item.lower()
            if "天气" in item and ("覆盖范围" in item or "天气源" in item):
                label = "我先检查了当前可用天气数据的范围"
            elif "维基百科" in item and (
                "摘要接口" in item or "query api" in lower or "备用语言站点" in item or "主站点" in item
            ):
                label = "我先尝试了当前可用的百科摘要来源和词条匹配方式"
            elif "汇率" in item and ("汇率源" in item or "货币对" in item):
                label = "我先检查了当前可用的汇率来源和货币对匹配方式"
            elif "时区" in item or "城市别名" in item:
                label = "我先按城市名称和标准时区名做了识别"
            else:
                label = item
            if label not in seen:
                seen.add(label)
                simplified.append(label)
        return "；".join(simplified)
