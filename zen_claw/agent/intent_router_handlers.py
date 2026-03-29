"""Routing registry and per-intent handlers for the intent router."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from zen_claw.agent.intent_router_contracts import (
    ExchangeRateResolution,
    IntentRouteResult,
    RecoveryBlocker,
    RecoveryPlan,
    RecoveryStrategy,
)
from zen_claw.agent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class IntentRegistryEntry:
    intent_name: str
    parser_name: str
    handler_name: str


class IntentRouterHandlersMixin:
    _ROUTE_REGISTRY = (
        IntentRegistryEntry("code_exec", "_extract_exec_request", "_handle_exec_request"),
        IntentRegistryEntry("weather", "_extract_weather_request", "_handle_weather_request"),
        IntentRegistryEntry("exchange_rate", "_extract_exchange_request", "_handle_exchange_request"),
        IntentRegistryEntry("fixed_site_fetch", "_extract_fixed_site_request", "_handle_fixed_site_request"),
        IntentRegistryEntry("time", "_extract_time_request", "_handle_time_request"),
        IntentRegistryEntry("direct_contracts", "_match_direct_contract", "_handle_direct_contract_request"),
    )

    def _iter_intent_registry(self) -> tuple[IntentRegistryEntry, ...]:
        return self._ROUTE_REGISTRY

    async def route(
        self,
        content: str,
        *,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        for entry in self._iter_intent_registry():
            parser = getattr(self, entry.parser_name)
            request = parser(content)
            if request is None:
                continue
            handler = getattr(self, entry.handler_name)
            result = handler(request, tools=tools, trace_id=trace_id)
            if inspect.isawaitable(result):
                return await result
            return result
        return IntentRouteResult(handled=False)

    def _handle_exec_request(self, request: dict[str, str], *, tools: ToolRegistry, trace_id: str) -> IntentRouteResult:
        del tools, trace_id
        return self._route_exec_request(request)

    async def _handle_weather_request(
        self, request: dict[str, str], *, tools: ToolRegistry, trace_id: str
    ) -> IntentRouteResult:
        return await self._route_weather(
            str(request.get("content") or ""),
            location=str(request.get("location") or ""),
            tools=tools,
            trace_id=trace_id,
        )

    async def _handle_exchange_request(
        self, request: dict[str, object], *, tools: ToolRegistry, trace_id: str
    ) -> IntentRouteResult:
        return await self._route_exchange(request, tools=tools, trace_id=trace_id)

    async def _handle_fixed_site_request(
        self, request: dict[str, str], *, tools: ToolRegistry, trace_id: str
    ) -> IntentRouteResult:
        return await self._route_fixed_site(request, tools=tools, trace_id=trace_id)

    def _handle_time_request(
        self, request: dict[str, str | None], *, tools: ToolRegistry, trace_id: str
    ) -> IntentRouteResult:
        del tools, trace_id
        return self._route_time(request)

    def _handle_direct_contract_request(self, request, *, tools: ToolRegistry, trace_id: str) -> IntentRouteResult:
        del tools, trace_id
        return self._route_direct_contract(request)

    def _route_direct_contract(self, request) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=request.intent_name,
            content=request.content,
            route_status="direct_success",
        )

    def _route_exec_request(self, request: dict[str, str]) -> IntentRouteResult:
        command = str(request.get("command") or "").strip()
        if not command:
            return IntentRouteResult(handled=False)
        return self._needs_explicit_approval_with_plan(
            intent_name="code_exec",
            summary="当前安全路径无法直接执行这条命令。",
            plan=RecoveryPlan(
                blocker=RecoveryBlocker(
                    kind="permission_required",
                    description="执行命令属于高风险能力，当前安全路径未持有授权",
                    missing_requirement="一次性显式授权的 exec 工具范围",
                ),
                strategies=[
                    RecoveryStrategy(
                        kind="guidance_only",
                        detail="先按最小范围请求一次性显式授权，再继续执行命令",
                    ),
                ],
                checked_scope=[
                    "当前直达执行路由已识别这是显式命令执行请求",
                    "当前直达执行路由已停留在未授权的安全路径内",
                ],
                next_steps=["如果你确认，我会只为这次请求发起一次性 exec 授权"],
                fallback_options=[
                    "如果你只是想知道命令怎么写，我也可以先给你命令建议而不实际执行",
                ],
            ),
            contract=self._EXEC_CONTRACT,
            diagnostic="explicit_approval:exec",
        )

    async def _route_exchange(
        self,
        request: dict[str, object],
        *,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        source = str(request["source"])
        target = str(request["target"])
        amount = float(request.get("amount") or 1.0)
        if source == target:
            return IntentRouteResult(
                handled=True,
                intent_name="exchange_rate",
                content=self._build_exchange_success_message(source, target, amount, 1.0),
                contract=self._EXCHANGE_CONTRACT,
                route_status="direct_success",
            )

        resolution = await self._run_source_fallback(
            [
                (
                    "er_api",
                    lambda: self._fetch_exchange_rate_primary(
                        source=source,
                        target=target,
                        tools=tools,
                        trace_id=trace_id,
                    ),
                ),
                (
                    "frankfurter",
                    lambda: self._fetch_exchange_rate_fallback(
                        source=source,
                        target=target,
                        tools=tools,
                        trace_id=trace_id,
                    ),
                ),
            ]
        )
        if isinstance(resolution.value, ExchangeRateResolution):
            content = self._build_exchange_success_message(
                source,
                target,
                amount,
                resolution.value.rate,
            )
            if resolution.value.recovery_kind != "direct":
                return self._direct_success_with_plan(
                    intent_name="exchange_rate",
                    content=content,
                    plan=self._build_exchange_resolution_plan(
                        source=source,
                        target=target,
                        recovery_kind=resolution.value.recovery_kind,
                    ),
                    contract=self._EXCHANGE_CONTRACT,
                    diagnostic=f"exchange_{resolution.value.recovery_kind}_resolved:{source}_{target}",
                )
            return self._direct_success(
                intent_name="exchange_rate",
                content=content,
                contract=self._EXCHANGE_CONTRACT,
            )

        outcome = self._build_exchange_failure_outcome(source, target)
        diagnostic = f"exchange_sources_failed:{source}_{target}:{','.join(resolution.attempts or [])}"
        if self.allow_runtime_constrained_replan and self._EXCHANGE_CONTRACT.allow_constrained_replan:
            return self._needs_constrained_replan_with_outcome(
                intent_name="exchange_rate",
                outcome=outcome,
                contract=self._EXCHANGE_CONTRACT,
                diagnostic=diagnostic,
            )
        return self._direct_failed_with_outcome(
            intent_name="exchange_rate",
            outcome=outcome,
            contract=self._EXCHANGE_CONTRACT,
            diagnostic=diagnostic,
        )

    def _route_time(self, request: dict[str, str | None]) -> IntentRouteResult:
        mode = str(request.get("mode") or "time")
        zone_key = request.get("timezone")
        label = request.get("label") or ""
        if zone_key:
            fuzzy_candidate = self._fuzzy_timezone_alias_lookup(zone_key)
            candidate = self._resolve_timezone_candidate(zone_key)
            if candidate is None and fuzzy_candidate is not None:
                candidate = self._TIMEZONE_ALIASES.get(fuzzy_candidate)
            zone = self._resolve_timezone(zone_key)
            if zone is None and candidate is not None:
                fallback_now = self._fallback_now_in_timezone(self._utc_now(), candidate)
                if fallback_now is not None:
                    content = self._format_time_response(mode=mode, now=fallback_now, label=label or candidate)
                    if fuzzy_candidate is not None:
                        return self._direct_success_with_plan(
                            intent_name="time",
                            content=content,
                            plan=self._build_timezone_fuzzy_alias_resolution_plan(),
                            contract=self._TIME_CONTRACT,
                            diagnostic=f"timezone_fuzzy_alias_resolved_via_fallback:{fuzzy_candidate}",
                        )
                    return self._direct_success_with_plan(
                        intent_name="time",
                        content=content,
                        plan=self._build_timezone_fallback_resolution_plan(),
                        contract=self._TIME_CONTRACT,
                        diagnostic=f"timezone_fallback_resolved:{candidate}",
                    )
            if zone is None:
                display = label or zone_key
                return self._direct_failed_with_plan(
                    intent_name="time",
                    summary=f"暂时无法识别“{display}”对应的时区，因此不能直接给出时间结果。",
                    plan=RecoveryPlan(
                        blocker=RecoveryBlocker(
                            kind="input_ambiguous",
                            description="时区映射无法确认",
                            missing_requirement="可确认的城市、地区或标准时区名",
                        ),
                        strategies=[
                            RecoveryStrategy(kind="local_correction", detail="继续按更明确的城市或标准时区名重试解析"),
                            RecoveryStrategy(kind="guidance_only", detail="先给出可继续推进的补充输入建议"),
                        ],
                        checked_scope=[
                            "当前直达时间路由已尝试按内置城市别名解析",
                            "当前直达时间路由已尝试按标准时区名解析",
                        ],
                        next_steps=[
                            "你可以直接给我标准时区名，例如 America/New_York",
                            "你也可以换成更明确的城市表达，例如纽约市、东京时间",
                        ],
                        fallback_options=[
                            "如果你只想知道现在几点，我也可以先告诉你当前时区时间",
                            "如果你补充国家或城市全名，我可以继续帮你判断",
                        ],
                    ),
                    contract=self._TIME_CONTRACT,
                    diagnostic=f"timezone_unrecognized:{display}",
                )
            now = self._utc_now().astimezone(zone)
            if fuzzy_candidate is not None:
                return self._direct_success_with_plan(
                    intent_name="time",
                    content=self._format_time_response(mode=mode, now=now, label=label or zone.key),
                    plan=self._build_timezone_fuzzy_alias_resolution_plan(),
                    contract=self._TIME_CONTRACT,
                    diagnostic=f"timezone_fuzzy_alias_resolved:{fuzzy_candidate}",
                )
            return self._direct_success(
                intent_name="time",
                content=self._format_time_response(mode=mode, now=now, label=label or zone.key),
                contract=self._TIME_CONTRACT,
            )

        now = self._utc_now().astimezone()
        return self._direct_success(
            intent_name="time",
            content=self._format_time_response(mode=mode, now=now, label="当前时区"),
            contract=self._TIME_CONTRACT,
        )

    async def _route_fixed_site(
        self,
        request: dict[str, str],
        *,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        topic = str(request.get("topic") or "").strip()
        if not topic:
            return IntentRouteResult(handled=False)
        site = str(request.get("site") or "wikipedia").strip().lower() or "wikipedia"
        languages = self._fixed_site_language_order(topic)
        resolution = await self._run_source_fallback(
            [
                (
                    f"{site}_{language}",
                    lambda language=language: self._fetch_wikipedia_summary(
                        language=language,
                        topic=topic,
                        tools=tools,
                        trace_id=trace_id,
                    ),
                )
                for language in languages
            ]
        )
        if isinstance(resolution.value, dict):
            content = self._build_fixed_site_success_message(site=site, payload=resolution.value)
            recovery_kind = str(resolution.value.get("_recovery_kind") or "direct").strip().lower()
            preferred_winner = f"{site}_{languages[0]}" if languages else ""
            if recovery_kind != "direct" or (
                resolution.winner and preferred_winner and resolution.winner != preferred_winner
            ):
                return self._direct_success_with_plan(
                    intent_name="fixed_site_fetch",
                    content=content,
                    plan=self._build_fixed_site_resolution_plan(
                        site=site,
                        recovery_kind=(recovery_kind if recovery_kind != "direct" else "fallback_source"),
                    ),
                    contract=self._FIXED_SITE_CONTRACT,
                    diagnostic=f"fixed_site_{recovery_kind}_resolved:{site}:{topic}:{resolution.winner or ''}",
                )
            return self._direct_success(
                intent_name="fixed_site_fetch",
                content=content,
                contract=self._FIXED_SITE_CONTRACT,
            )
        outcome = self._build_fixed_site_failure_outcome(site=site, topic=topic)
        diagnostic = f"fixed_site_failed:{site}:{topic}:{','.join(resolution.attempts or [])}"
        if self.allow_runtime_constrained_replan and self._FIXED_SITE_CONTRACT.allow_constrained_replan:
            return self._needs_constrained_replan_with_outcome(
                intent_name="fixed_site_fetch",
                outcome=outcome,
                contract=self._FIXED_SITE_CONTRACT,
                diagnostic=diagnostic,
            )
        return self._direct_failed_with_outcome(
            intent_name="fixed_site_fetch",
            outcome=outcome,
            contract=self._FIXED_SITE_CONTRACT,
            diagnostic=diagnostic,
        )

    async def _route_weather(
        self,
        content: str,
        *,
        location: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        days = self._extract_weather_days(content)
        if days > self._MAX_FORECAST_DAYS and self._should_route_recent_weather_to_history(content):
            history_lines = await self._fetch_open_meteo_historical_weather_lines(
                location=location,
                days=days,
                tools=tools,
                trace_id=trace_id,
            )
            if history_lines:
                response = f"{location}最近{days}天天气记录：\n" + "\n".join(history_lines)
                return self._direct_success_with_plan(
                    intent_name="weather",
                    content=response,
                    plan=self._build_weather_history_resolution_plan(days=days),
                    contract=self._WEATHER_CONTRACT,
                    diagnostic=f"weather_history_resolved:{days}",
                )
        if days > self._MAX_FORECAST_DAYS:
            return self._direct_failed_with_outcome(
                intent_name="weather",
                outcome=self._build_weather_days_limit_outcome(
                    location,
                    requested_days=days,
                    max_supported_days=self._MAX_FORECAST_DAYS,
                    request_scope="future" if "未来" in content else "recent",
                ),
                contract=self._WEATHER_CONTRACT,
                diagnostic=f"weather_days_exceed_limit:{days}",
            )
        resolution = await self._run_source_fallback(
            [
                (
                    "wttr",
                    lambda: self._fetch_wttr_weather_lines(
                        location=location,
                        days=days,
                        tools=tools,
                        trace_id=trace_id,
                    ),
                ),
                (
                    "open_meteo",
                    lambda: self._fetch_open_meteo_weather_lines(
                        location=location,
                        days=days,
                        tools=tools,
                        trace_id=trace_id,
                    ),
                ),
            ]
        )
        if isinstance(resolution.value, list) and resolution.value:
            weather_content = f"{location}天气预报：\n" + "\n".join(resolution.value)
            if resolution.winner == "open_meteo":
                return self._direct_success_with_plan(
                    intent_name="weather",
                    content=weather_content,
                    plan=self._build_weather_fallback_source_resolution_plan(location=location, days=days),
                    contract=self._WEATHER_CONTRACT,
                    diagnostic=f"weather_fallback_source_resolved:{location}:{days}",
                )
            return self._direct_success(
                intent_name="weather",
                content=weather_content,
                contract=self._WEATHER_CONTRACT,
            )

        failure_plan = self._build_weather_source_failure_plan(location=location)
        diagnostic = f"weather_sources_failed:{','.join(resolution.attempts or [])}"
        if self.allow_runtime_constrained_replan and self._WEATHER_CONTRACT.allow_constrained_replan:
            return self._needs_constrained_replan_with_plan(
                intent_name="weather",
                summary=f"暂时无法获取{location}的天气数据。",
                plan=failure_plan,
                contract=self._WEATHER_CONTRACT,
                diagnostic=diagnostic,
            )
        return self._direct_failed_with_plan(
            intent_name="weather",
            summary=f"暂时无法获取{location}的天气数据。",
            plan=failure_plan,
            contract=self._WEATHER_CONTRACT,
            diagnostic=diagnostic,
        )

    @classmethod
    def _build_exchange_success_message(
        cls, source: str, target: str, amount: float, rate: float
    ) -> str:
        total = amount * rate
        source_label = cls._CURRENCY_LABELS.get(source, source)
        target_label = cls._CURRENCY_LABELS.get(target, target)
        amount_text = cls._format_number(amount)
        total_text = cls._format_number(total)
        rate_text = cls._format_number(rate, precision=6)
        return (
            f"{amount_text}{source_label} ≈ {total_text}{target_label}。"
            f"参考汇率：1 {source} = {rate_text} {target}。"
        )

    @staticmethod
    def _fixed_site_language_order(topic: str) -> list[str]:
        if re.search(r"[\u4e00-\u9fff]", topic):
            return ["zh", "en"]
        return ["en", "zh"]

    @staticmethod
    def _build_fixed_site_success_message(*, site: str, payload: dict[str, str]) -> str:
        title = str(payload.get("title") or "").strip()
        extract = str(payload.get("extract") or "").strip()
        language = str(payload.get("language") or "").strip().lower()
        site_label = "维基百科" if site == "wikipedia" else site
        language_label = {"zh": "中文", "en": "英文"}.get(language, language or "默认")
        prefix = f"{site_label}{language_label}摘要"
        if title:
            prefix += f"（{title}）"
        return f"{prefix}：{extract}"

    async def _fetch_weather_payload_text(
        self,
        *,
        location: str,
        tools: ToolRegistry,
        trace_id: str,
    ):
        params = {
            "url": f"https://wttr.in/{quote(location)}?format=j1",
            "extractMode": "text",
            "maxChars": 80000,
        }
        return await self._execute_with_retry(
            tools=tools,
            params=params,
            trace_id=trace_id,
            policy=self._LOW_RISK_FETCH_RETRY,
        )

    async def _fetch_wttr_weather_lines(
        self,
        *,
        location: str,
        days: int,
        tools: ToolRegistry,
        trace_id: str,
    ) -> list[str] | None:
        wttr_result = await self._fetch_weather_payload_text(
            location=location,
            tools=tools,
            trace_id=trace_id,
        )
        if not wttr_result.ok:
            return None
        weather_payload = self._extract_weather_payload(wttr_result.content)
        if not isinstance(weather_payload, dict):
            return None
        lines = self._build_wttr_weather_lines(weather_payload, days=days)
        return lines if len(lines) >= days else None

    async def _fetch_open_meteo_weather_lines(
        self,
        *,
        location: str,
        days: int,
        tools: ToolRegistry,
        trace_id: str,
    ) -> list[str]:
        location_meta = await self._fetch_open_meteo_location_meta(
            location=location,
            tools=tools,
            trace_id=trace_id,
        )
        if location_meta is None:
            return []
        latitude, longitude, timezone_name = location_meta
        forecast_result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    "https://api.open-meteo.com/v1/forecast"
                    f"?latitude={latitude}&longitude={longitude}"
                    "&daily=weather_code,temperature_2m_max,temperature_2m_min"
                    f"&forecast_days={days}&timezone={quote(timezone_name)}"
                ),
                "extractMode": "text",
                "maxChars": 12000,
            },
            trace_id=trace_id,
        )
        if not forecast_result.ok:
            return []
        forecast_payload = self._extract_json_object(forecast_result.content)
        daily = forecast_payload.get("daily")
        if not isinstance(daily, dict):
            return []
        return self._build_open_meteo_daily_lines(daily, days=days)

    async def _fetch_open_meteo_historical_weather_lines(
        self,
        *,
        location: str,
        days: int,
        tools: ToolRegistry,
        trace_id: str,
    ) -> list[str]:
        location_meta = await self._fetch_open_meteo_location_meta(
            location=location,
            tools=tools,
            trace_id=trace_id,
        )
        if location_meta is None:
            return []
        latitude, longitude, timezone_name = location_meta
        end_date = self._utc_now().date()
        start_date = end_date - timedelta(days=max(0, days - 1))
        history_result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    "https://archive-api.open-meteo.com/v1/archive"
                    f"?latitude={latitude}&longitude={longitude}"
                    "&daily=weather_code,temperature_2m_max,temperature_2m_min"
                    f"&start_date={start_date.isoformat()}"
                    f"&end_date={end_date.isoformat()}"
                    f"&timezone={quote(timezone_name)}"
                ),
                "extractMode": "text",
                "maxChars": 16000,
            },
            trace_id=trace_id,
        )
        if not history_result.ok:
            return []
        history_payload = self._extract_json_object(history_result.content)
        daily = history_payload.get("daily")
        if not isinstance(daily, dict):
            return []
        return self._build_open_meteo_daily_lines(daily, days=days)

    async def _fetch_open_meteo_location_meta(
        self,
        *,
        location: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> tuple[float, float, str] | None:
        geo_result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    "https://geocoding-api.open-meteo.com/v1/search"
                    f"?name={quote(location)}&count=1&language=zh&format=json"
                ),
                "extractMode": "text",
                "maxChars": 12000,
            },
            trace_id=trace_id,
        )
        if not geo_result.ok:
            return None
        geo_payload = self._extract_json_object(geo_result.content)
        results = geo_payload.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0] if isinstance(results[0], dict) else {}
        latitude = first.get("latitude")
        longitude = first.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            return None
        timezone_name = str(first.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        return (float(latitude), float(longitude), timezone_name)

    def _build_open_meteo_daily_lines(self, daily: dict[str, Any], *, days: int) -> list[str]:
        dates = daily.get("time")
        codes = daily.get("weather_code")
        highs = daily.get("temperature_2m_max")
        lows = daily.get("temperature_2m_min")
        if not all(isinstance(item, list) for item in (dates, codes, highs, lows)):
            return []
        lines: list[str] = []
        count = min(len(dates), len(codes), len(highs), len(lows), days)
        for idx in range(count):
            date = str(dates[idx] or "").strip()
            if not date:
                continue
            desc = self._open_meteo_weather_desc(codes[idx])
            high = self._format_temperature(highs[idx])
            low = self._format_temperature(lows[idx])
            parts = [date]
            if desc:
                parts.append(desc)
            if high or low:
                parts.append(f"{low}~{high}°C" if high and low else f"{high or low}°C")
            lines.append(" ".join(parts))
        return lines

    async def _fetch_exchange_rate_primary(
        self,
        *,
        source: str,
        target: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> ExchangeRateResolution | None:
        result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": f"https://open.er-api.com/v6/latest/{quote(source)}",
                "extractMode": "text",
                "maxChars": 16000,
            },
            trace_id=trace_id,
        )
        if not result.ok:
            return None
        payload = self._extract_json_object(result.content)
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            return None
        value = rates.get(target)
        if not isinstance(value, (int, float)):
            return None
        return ExchangeRateResolution(rate=float(value))

    async def _fetch_exchange_rate_fallback(
        self,
        *,
        source: str,
        target: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> ExchangeRateResolution | None:
        direct_value = await self._fetch_frankfurter_rate(
            source=source,
            target=target,
            tools=tools,
            trace_id=trace_id,
        )
        if direct_value is not None:
            return ExchangeRateResolution(rate=direct_value, recovery_kind="fallback_source")
        reverse_value = await self._fetch_frankfurter_rate(
            source=target,
            target=source,
            tools=tools,
            trace_id=trace_id,
        )
        if isinstance(reverse_value, (int, float)) and reverse_value not in {0, 0.0}:
            return ExchangeRateResolution(rate=1.0 / float(reverse_value), recovery_kind="reverse_solve")
        return None

    async def _fetch_frankfurter_rate(
        self,
        *,
        source: str,
        target: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> float | None:
        result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    "https://api.frankfurter.app/latest"
                    f"?from={quote(source)}&to={quote(target)}"
                ),
                "extractMode": "text",
                "maxChars": 12000,
            },
            trace_id=trace_id,
        )
        if not result.ok:
            return None
        payload = self._extract_json_object(result.content)
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            return None
        value = rates.get(target)
        if not isinstance(value, (int, float)):
            return None
        return float(value)

    async def _fetch_wikipedia_summary(
        self,
        *,
        language: str,
        topic: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> dict[str, str] | None:
        direct = await self._fetch_wikipedia_summary_once(
            language=language,
            topic=topic,
            tools=tools,
            trace_id=trace_id,
        )
        if direct is not None:
            return direct
        resolved_topic = await self._search_wikipedia_topic(
            language=language,
            topic=topic,
            tools=tools,
            trace_id=trace_id,
        )
        if resolved_topic and resolved_topic != topic:
            resolved = await self._fetch_wikipedia_summary_once(
                language=language,
                topic=resolved_topic,
                tools=tools,
                trace_id=trace_id,
            )
            if resolved is not None:
                enriched = dict(resolved)
                enriched["_recovery_kind"] = "same_site_search"
                return enriched
        return None

    async def _fetch_wikipedia_summary_once(
        self,
        *,
        language: str,
        topic: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> dict[str, str] | None:
        result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{quote(topic)}",
                "extractMode": "text",
                "maxChars": 24000,
            },
            trace_id=trace_id,
        )
        if result.ok:
            payload = self._extract_json_object(result.content)
            extract = str(payload.get("extract") or "").strip()
            title = str(payload.get("title") or topic).strip() or topic
            if extract:
                return {
                    "site": "wikipedia",
                    "language": language,
                    "title": title,
                    "extract": extract,
                    "_recovery_kind": "direct",
                }
        return await self._fetch_wikipedia_summary_via_query_api(
            language=language,
            topic=topic,
            tools=tools,
            trace_id=trace_id,
        )

    async def _search_wikipedia_topic(
        self,
        *,
        language: str,
        topic: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> str | None:
        result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    f"https://{language}.wikipedia.org/w/api.php"
                    "?action=query"
                    "&list=search"
                    "&srwhat=text"
                    "&srlimit=1"
                    "&format=json"
                    "&formatversion=2"
                    f"&srsearch={quote(topic)}"
                ),
                "extractMode": "text",
                "maxChars": 16000,
            },
            trace_id=trace_id,
        )
        if not result.ok:
            return None
        payload = self._extract_json_object(result.content)
        query = payload.get("query")
        if not isinstance(query, dict):
            return None
        searchinfo = query.get("searchinfo")
        if isinstance(searchinfo, dict):
            suggestion = str(searchinfo.get("suggestion") or "").strip()
            if suggestion:
                return self._normalize_wikipedia_title_candidate(suggestion)
        search = query.get("search")
        if not isinstance(search, list) or not search:
            return None
        first = next((item for item in search if isinstance(item, dict)), None)
        if not isinstance(first, dict):
            return None
        title = str(first.get("title") or "").strip()
        return self._normalize_wikipedia_title_candidate(title) or None

    @staticmethod
    def _normalize_wikipedia_title_candidate(title: str) -> str:
        text = str(title or "").strip()
        if not text:
            return ""
        if re.fullmatch(r"[a-z0-9'().,_ -]+", text):
            return " ".join(part.capitalize() for part in text.split())
        return text

    async def _fetch_wikipedia_summary_via_query_api(
        self,
        *,
        language: str,
        topic: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> dict[str, str] | None:
        result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    f"https://{language}.wikipedia.org/w/api.php"
                    "?action=query"
                    "&prop=extracts"
                    "&exintro=1"
                    "&explaintext=1"
                    "&redirects=1"
                    "&format=json"
                    "&formatversion=2"
                    f"&titles={quote(topic)}"
                ),
                "extractMode": "text",
                "maxChars": 24000,
            },
            trace_id=trace_id,
        )
        if not result.ok:
            return None
        payload = self._extract_json_object(result.content)
        query = payload.get("query")
        if not isinstance(query, dict):
            return None
        pages = query.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        first = next((page for page in pages if isinstance(page, dict)), None)
        if not isinstance(first, dict):
            return None
        extract = str(first.get("extract") or "").strip()
        title = str(first.get("title") or topic).strip() or topic
        if not extract:
            return None
        return {
            "site": "wikipedia",
            "language": language,
            "title": title,
            "extract": extract,
            "_recovery_kind": "fallback_source",
        }
