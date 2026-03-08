"""Intent router for high-certainty pre-LLM utility requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from zen_claw.agent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class IntentToolContract:
    """Runtime-enforced tool contract for a recognized intent."""

    intent_name: str
    preferred_tools: list[str]
    allowed_tools: set[str]
    denied_tools: set[str]
    version: int = 1
    intent_mode: Literal["router_first", "skill_first", "hybrid"] = "skill_first"
    allow_constrained_replan: bool = True
    allow_high_risk_escalation: bool = False
    response_mode: Literal["direct", "llm_assisted"] = "direct"
    failure_mode: Literal["runtime_direct", "runtime_fact_llm_format"] = "runtime_direct"
    fact_payload_schema: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "IntentToolContract | None":
        """Build a runtime contract from structured metadata."""
        if not isinstance(payload, dict):
            return None
        intent_name = str(payload.get("intent") or payload.get("intent_name") or "").strip()
        version = payload.get("version", 1)
        if not isinstance(version, int) or version < 1:
            return None
        intent_mode = str(payload.get("intent_mode") or "skill_first").strip().lower()
        if intent_mode not in {"router_first", "skill_first", "hybrid"}:
            return None
        preferred_tools = cls._normalize_tool_list(payload.get("preferred_tools"))
        allowed_tools = set(cls._normalize_tool_list(payload.get("allowed_tools")))
        denied_tools = set(cls._normalize_tool_list(payload.get("denied_tools")))
        response_mode = str(payload.get("response_mode") or "direct").strip().lower()
        if response_mode not in {"direct", "llm_assisted"}:
            return None
        failure_mode = str(payload.get("failure_mode") or "runtime_direct").strip().lower()
        if failure_mode not in {"runtime_direct", "runtime_fact_llm_format"}:
            return None
        fact_payload_schema = payload.get("fact_payload_schema")
        if fact_payload_schema is not None and not isinstance(fact_payload_schema, dict):
            return None
        if not intent_name or not allowed_tools:
            return None
        if preferred_tools:
            preferred_tools = [tool for tool in preferred_tools if tool in allowed_tools]
        if not preferred_tools:
            preferred_tools = sorted(allowed_tools)
        return cls(
            intent_name=intent_name,
            version=version,
            intent_mode=intent_mode,
            preferred_tools=preferred_tools,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            allow_constrained_replan=bool(payload.get("allow_constrained_replan", True)),
            allow_high_risk_escalation=bool(payload.get("allow_high_risk_escalation", False)),
            response_mode=response_mode,
            failure_mode=failure_mode,
            fact_payload_schema=fact_payload_schema if isinstance(fact_payload_schema, dict) else None,
        )

    @staticmethod
    def _normalize_tool_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item or "").strip().lower()
            if text and text not in out:
                out.append(text)
        return out


@dataclass
class IntentRouteResult:
    """Outcome of a pre-LLM routing attempt."""

    handled: bool
    intent_name: str | None = None
    content: str | None = None
    contract: IntentToolContract | None = None
    route_status: Literal[
        "miss",
        "direct_success",
        "direct_failed",
        "needs_constrained_replan",
        "needs_explicit_approval",
    ] = "miss"
    diagnostic: str | None = None
    skip_planning: bool = False


@dataclass
class SourceFallbackResult:
    """Result of trying an ordered list of low-risk data sources."""

    value: Any | None
    winner: str | None = None
    attempts: list[str] | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Minimal retry policy for low-risk direct intent fetches."""

    max_attempts: int = 2


class IntentRouter:
    """Handle a narrow set of deterministic, low-risk intents before LLM planning."""

    _MAX_FORECAST_DAYS = 16
    _CURRENCY_ALIASES = {
        "usd": "USD",
        "us dollar": "USD",
        "dollar": "USD",
        "美元": "USD",
        "美金": "USD",
        "cny": "CNY",
        "rmb": "CNY",
        "renminbi": "CNY",
        "人民币": "CNY",
        "元": "CNY",
        "eur": "EUR",
        "euro": "EUR",
        "欧元": "EUR",
        "jpy": "JPY",
        "yen": "JPY",
        "日元": "JPY",
        "日币": "JPY",
        "gbp": "GBP",
        "pound": "GBP",
        "英镑": "GBP",
        "hkd": "HKD",
        "港币": "HKD",
        "港元": "HKD",
        "cad": "CAD",
        "加元": "CAD",
        "aud": "AUD",
        "澳元": "AUD",
        "sgd": "SGD",
        "新加坡元": "SGD",
        "krw": "KRW",
        "韩元": "KRW",
        "chf": "CHF",
        "瑞士法郎": "CHF",
    }
    _CURRENCY_LABELS = {
        "USD": "美元",
        "CNY": "人民币",
        "EUR": "欧元",
        "JPY": "日元",
        "GBP": "英镑",
        "HKD": "港币",
        "CAD": "加元",
        "AUD": "澳元",
        "SGD": "新加坡元",
        "KRW": "韩元",
        "CHF": "瑞士法郎",
    }
    _TIMEZONE_ALIASES = {
        "北京时间": "Asia/Shanghai",
        "中国时间": "Asia/Shanghai",
        "上海时间": "Asia/Shanghai",
        "上海": "Asia/Shanghai",
        "北京": "Asia/Shanghai",
        "北京时间区": "Asia/Shanghai",
        "utc": "UTC",
        "gmt": "UTC",
        "世界协调时": "UTC",
        "伦敦": "Europe/London",
        "伦敦时间": "Europe/London",
        "东京": "Asia/Tokyo",
        "东京时间": "Asia/Tokyo",
        "首尔": "Asia/Seoul",
        "首尔时间": "Asia/Seoul",
        "新加坡": "Asia/Singapore",
        "新加坡时间": "Asia/Singapore",
        "香港": "Asia/Hong_Kong",
        "香港时间": "Asia/Hong_Kong",
        "纽约": "America/New_York",
        "纽约时间": "America/New_York",
        "洛杉矶": "America/Los_Angeles",
        "洛杉矶时间": "America/Los_Angeles",
        "旧金山": "America/Los_Angeles",
        "旧金山时间": "America/Los_Angeles",
        "西雅图": "America/Los_Angeles",
        "西雅图时间": "America/Los_Angeles",
        "芝加哥": "America/Chicago",
        "芝加哥时间": "America/Chicago",
        "巴黎": "Europe/Paris",
        "巴黎时间": "Europe/Paris",
        "柏林": "Europe/Berlin",
        "柏林时间": "Europe/Berlin",
        "悉尼": "Australia/Sydney",
        "悉尼时间": "Australia/Sydney",
        "纽约市": "America/New_York",
        "tokyo": "Asia/Tokyo",
        "new york": "America/New_York",
        "newyork": "America/New_York",
        "los angeles": "America/Los_Angeles",
        "san francisco": "America/Los_Angeles",
        "seattle": "America/Los_Angeles",
        "chicago": "America/Chicago",
        "london": "Europe/London",
        "paris": "Europe/Paris",
        "berlin": "Europe/Berlin",
        "sydney": "Australia/Sydney",
        "singapore": "Asia/Singapore",
        "hong kong": "Asia/Hong_Kong",
        "seoul": "Asia/Seoul",
    }
    _WEATHER_CONTRACT = IntentToolContract(
        intent_name="weather",
        intent_mode="router_first",
        preferred_tools=["web_fetch"],
        allowed_tools={"web_fetch"},
        denied_tools={"exec", "spawn", "write_file", "edit_file"},
        allow_constrained_replan=True,
        allow_high_risk_escalation=False,
        response_mode="direct",
        failure_mode="runtime_direct",
        fact_payload_schema={
            "type": "object",
            "fields": ["location", "requested_days", "max_supported_days", "reason"],
        },
    )
    _TIME_CONTRACT = IntentToolContract(
        intent_name="time",
        intent_mode="router_first",
        preferred_tools=[],
        allowed_tools=set(),
        denied_tools={"exec", "spawn", "write_file", "edit_file", "web_fetch"},
        allow_constrained_replan=False,
        allow_high_risk_escalation=False,
        response_mode="direct",
        failure_mode="runtime_direct",
    )
    _EXCHANGE_CONTRACT = IntentToolContract(
        intent_name="exchange_rate",
        intent_mode="router_first",
        preferred_tools=["web_fetch"],
        allowed_tools={"web_fetch"},
        denied_tools={"exec", "spawn", "write_file", "edit_file"},
        allow_constrained_replan=True,
        allow_high_risk_escalation=False,
        response_mode="direct",
        failure_mode="runtime_direct",
    )
    _FIXED_SITE_CONTRACT = IntentToolContract(
        intent_name="fixed_site_fetch",
        intent_mode="router_first",
        preferred_tools=["web_fetch"],
        allowed_tools={"web_fetch"},
        denied_tools={"exec", "spawn", "write_file", "edit_file"},
        allow_constrained_replan=True,
        allow_high_risk_escalation=False,
        response_mode="direct",
        failure_mode="runtime_direct",
    )
    _LOW_RISK_FETCH_RETRY = RetryPolicy(max_attempts=2)

    async def route(
        self,
        content: str,
        *,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        location = self._extract_weather_location(content)
        if location:
            return await self._route_weather(content, location=location, tools=tools, trace_id=trace_id)
        exchange_request = self._extract_exchange_request(content)
        if exchange_request is not None:
            return await self._route_exchange(exchange_request, tools=tools, trace_id=trace_id)
        fixed_site_request = self._extract_fixed_site_request(content)
        if fixed_site_request is not None:
            return await self._route_fixed_site(fixed_site_request, tools=tools, trace_id=trace_id)
        time_request = self._extract_time_request(content)
        if time_request is not None:
            return self._route_time(time_request)
        return IntentRouteResult(handled=False)

    @staticmethod
    def _direct_failed(
        *,
        intent_name: str,
        content: str,
        contract: IntentToolContract,
        diagnostic: str,
    ) -> IntentRouteResult:
        return IntentRouteResult(
            handled=True,
            intent_name=intent_name,
            content=content,
            contract=contract,
            route_status="direct_failed",
            diagnostic=diagnostic,
        )

    async def _route_exchange(
        self,
        request: dict[str, Any],
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
        if isinstance(resolution.value, (int, float)):
            return IntentRouteResult(
                handled=True,
                intent_name="exchange_rate",
                content=self._build_exchange_success_message(source, target, amount, float(resolution.value)),
                contract=self._EXCHANGE_CONTRACT,
                route_status="direct_success",
            )

        return self._direct_failed(
            intent_name="exchange_rate",
            content=self._build_exchange_failure_message(source, target),
            contract=self._EXCHANGE_CONTRACT,
            diagnostic=(
                f"exchange_sources_failed:{source}_{target}:"
                f"{','.join(resolution.attempts or [])}"
            ),
        )

    def _route_time(self, request: dict[str, str | None]) -> IntentRouteResult:
        mode = str(request.get("mode") or "time")
        zone_key = request.get("timezone")
        label = request.get("label") or ""

        if zone_key:
            zone = self._resolve_timezone(zone_key)
            if zone is None:
                display = label or zone_key
                return self._direct_failed(
                    intent_name="time",
                    content=(
                        f"暂时无法识别“{display}”对应的时区，因此不能直接给出时间结果。"
                        "这不是权限或审批问题，请改用明确城市或标准时区名再试一次。"
                    ),
                    contract=self._TIME_CONTRACT,
                    diagnostic=f"timezone_unrecognized:{display}",
                )
            now = self._utc_now().astimezone(zone)
            return IntentRouteResult(
                handled=True,
                intent_name="time",
                content=self._format_time_response(mode=mode, now=now, label=label or zone.key),
                contract=self._TIME_CONTRACT,
                route_status="direct_success",
            )

        now = self._utc_now().astimezone()
        return IntentRouteResult(
            handled=True,
            intent_name="time",
            content=self._format_time_response(mode=mode, now=now, label="当前时区"),
            contract=self._TIME_CONTRACT,
            route_status="direct_success",
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
            return IntentRouteResult(
                handled=True,
                intent_name="fixed_site_fetch",
                content=self._build_fixed_site_success_message(site=site, payload=resolution.value),
                contract=self._FIXED_SITE_CONTRACT,
                route_status="direct_success",
            )

        return self._direct_failed(
            intent_name="fixed_site_fetch",
            content=self._build_fixed_site_failure_message(site=site, topic=topic),
            contract=self._FIXED_SITE_CONTRACT,
            diagnostic=f"fixed_site_failed:{site}:{topic}:{','.join(resolution.attempts or [])}",
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
        if days > self._MAX_FORECAST_DAYS:
            return self._direct_failed(
                intent_name="weather",
                content=self._build_weather_days_limit_message(
                    location,
                    requested_days=days,
                    max_supported_days=self._MAX_FORECAST_DAYS,
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
            return IntentRouteResult(
                handled=True,
                intent_name="weather",
                content=f"{location}天气预报：\n" + "\n".join(resolution.value),
                contract=self._WEATHER_CONTRACT,
                route_status="direct_success",
            )

        return self._direct_failed(
            intent_name="weather",
            content=self._build_weather_failure_message(location),
            contract=self._WEATHER_CONTRACT,
            diagnostic=f"weather_sources_failed:{','.join(resolution.attempts or [])}",
        )

    @classmethod
    def _extract_weather_location(cls, content: str) -> str | None:
        if not content:
            return None
        text = re.split(r"[，,。！？!?；;]", content.strip(), maxsplit=1)[0]
        lowered = text.lower()
        if not any(token in lowered for token in ("天气", "weather", "forecast")):
            return None

        patterns = (
            r"(?P<loc>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-zA-Za-z\s·\-.]{0,40}?)(?:(?:最近|未来|近)?\d+天|最近一周|未来一周|这一周|本周|近两周|未来两周|两周|今天天气|今日天气|今天|今日)?的?天气",
            r"(?:weather|forecast)(?:\s+(?:for|in))?\s+(?P<loc>[A-Za-z][A-Za-z\s\-.]{1,40})",
            r"(?P<loc>[A-Za-z][A-Za-z\s\-.]{1,40})\s+(?:weather|forecast)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            location = str(match.group("loc") or "").strip()
            location = re.sub(
                r"^(告诉我|帮我|请|查询|查一下|查下|查查|看看|我想知道|我想看|麻烦你|想知道)",
                "",
                location,
            ).strip()
            location = re.sub(
                r"((?:最近|未来|近)?\d+天|最近一周|未来一周|这一周|本周|近两周|未来两周|两周|一周|最近|未来|最)$",
                "",
                location,
            )
            location = location.strip(" 的天气forecastweather")
            if location:
                return location
        return None

    @staticmethod
    def _extract_weather_days(content: str) -> int:
        text = content.lower()
        digit_match = re.search(r"(\d{1,3})\s*天", text)
        if digit_match:
            try:
                return max(1, int(digit_match.group(1)))
            except ValueError:
                pass
        if any(token in text for token in ("最近两周", "未来两周", "两周", "14-day", "14 day")):
            return 14
        if any(token in text for token in ("最近一周", "未来一周", "这一周", "本周", "7天", "7-day", "week")):
            return 7
        return 3

    @classmethod
    def _extract_exchange_request(cls, content: str) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if not any(
            token in lowered for token in ("汇率", "兑换", "换成", "换算", "rate", "exchange", "兑")
        ):
            return None

        mentions = cls._extract_currency_mentions(text)
        if len(mentions) < 2:
            return None
        source = mentions[0]
        target = next((code for code in mentions[1:] if code != source), None)
        if target is None:
            return None

        amount = 1.0
        amount_match = re.search(r"(\d+(?:\.\d+)?)", text[: text.find(source)] if source in text else text)
        if amount_match:
            try:
                amount = float(amount_match.group(1))
            except ValueError:
                amount = 1.0
        else:
            pair_match = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:美元|美金|人民币|元|欧元|日元|英镑|港币|港元|加元|澳元|新加坡元|韩元|瑞士法郎|usd|cny|eur|jpy|gbp|hkd|cad|aud|sgd|krw|chf)",
                lowered,
            )
            if pair_match:
                try:
                    amount = float(pair_match.group(1))
                except ValueError:
                    amount = 1.0
        return {"source": source, "target": target, "amount": amount}

    @classmethod
    def _extract_currency_mentions(cls, text: str) -> list[str]:
        alias_pattern = "|".join(
            sorted((re.escape(alias) for alias in cls._CURRENCY_ALIASES), key=len, reverse=True)
        )
        matches: list[tuple[int, str]] = []
        for match in re.finditer(alias_pattern, text, flags=re.IGNORECASE):
            alias = match.group(0).lower()
            code = cls._CURRENCY_ALIASES.get(alias)
            if code:
                matches.append((match.start(), code))
        ordered: list[str] = []
        for _, code in sorted(matches, key=lambda item: item[0]):
            if code not in ordered:
                ordered.append(code)
        return ordered

    @staticmethod
    def _extract_fixed_site_request(content: str) -> dict[str, str] | None:
        text = str(content or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if not any(token in lowered for token in ("wikipedia", "wiki", "维基百科")):
            return None

        patterns = (
            r"(?:wikipedia|wiki)\s+(?:summary|about|for)?\s*(?P<topic>[A-Za-z0-9][A-Za-z0-9\s().,_-]{1,80})",
            r"(?:在|用)?维基百科(?:上)?(?:介绍|查询|查一下|查下|看看|搜索)?(?P<topic>[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9\s·().,_-]{1,60})",
            r"(?P<topic>[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9\s·().,_-]{1,60})(?:的)?(?:维基百科|wikipedia|wiki)(?:摘要|介绍|词条)?",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            topic = str(match.group("topic") or "").strip()
            topic = re.sub(
                r"^(请|请问|帮我|告诉我|查一下|查下|看看|搜索|介绍一下|介绍|关于)",
                "",
                topic,
                flags=re.IGNORECASE,
            ).strip()
            topic = re.sub(r"\s+", " ", topic).strip(" ，,。.!?？；;")
            if topic:
                return {"site": "wikipedia", "topic": topic}
        return None

    @classmethod
    def _extract_time_request(cls, content: str) -> dict[str, str | None] | None:
        text = str(content or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if not any(
            token in lowered
            for token in (
                "几点",
                "时间",
                "日期",
                "几号",
                "星期几",
                "day",
                "date",
                "time",
                "timezone",
            )
        ):
            return None

        mode = "time"
        if any(token in lowered for token in ("日期", "几号", "date")):
            mode = "date"
        elif any(token in lowered for token in ("星期几", "周几", "day")):
            mode = "weekday"

        timezone_label = cls._extract_time_timezone_label(text)
        return {"mode": mode, "timezone": timezone_label, "label": timezone_label}

    @classmethod
    def _extract_time_timezone_label(cls, text: str) -> str | None:
        normalized = text.strip()
        explicit_patterns = (
            r"(?P<tz>[A-Za-z]+/[A-Za-z_+-]+)\s*(?:现在)?(?:时间|日期|几点|time|date|timezone)",
            r"(?:time|date|timezone)(?:\s+in|\s+for)?\s+(?P<tz>[A-Za-z]+/[A-Za-z_+-]+)",
            r"(?P<tz>UTC[+-]\d{1,2}(?::\d{2})?)\s*(?:现在)?(?:时间|日期|几点|time|date)",
        )
        for pattern in explicit_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return str(match.group("tz") or "").strip()

        alias_patterns = (
            r"(?P<label>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z\s]{0,20}?)(?:现在)?(?:时间|日期|几点|是几号|星期几)",
            r"(?:time|date)(?:\s+in|\s+for)?\s+(?P<label>[A-Za-z][A-Za-z\s]{1,24})",
        )
        for pattern in alias_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if not match:
                continue
            label = str(match.group("label") or "").strip()
            label = re.sub(
                r"^(请告诉我|请问|告诉我|帮我|请|现在|当前|查一下|查下|看看|麻烦你|告诉下|我想知道)",
                "",
                label,
            ).strip()
            label = re.sub(r"\s+", " ", label)
            label = label.removesuffix("现在").strip()
            if label in {"", "告诉我", "帮我", "请问", "请", "现在", "当前"}:
                return None
            if label:
                return label
        if any(token in normalized.lower() for token in ("现在几点", "现在时间", "今天几号", "今天日期", "今天星期几")):
            return None
        return None

    @classmethod
    def _resolve_timezone(cls, value: str) -> ZoneInfo | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw.strip()
        alias = (
            cls._TIMEZONE_ALIASES.get(normalized)
            or cls._TIMEZONE_ALIASES.get(normalized.lower())
            or cls._TIMEZONE_ALIASES.get(cls._normalize_timezone_alias_key(normalized))
        )
        if alias is None:
            normalized_key = cls._normalize_timezone_alias_key(normalized)
            for alias_key in sorted(cls._TIMEZONE_ALIASES, key=len, reverse=True):
                if alias_key and cls._normalize_timezone_alias_key(alias_key) in normalized_key:
                    alias = cls._TIMEZONE_ALIASES[alias_key]
                    break
        candidate = alias or normalized
        utc_offset = re.fullmatch(r"UTC(?P<sign>[+-])(?P<hours>\d{1,2})(?::(?P<minutes>\d{2}))?", candidate, re.IGNORECASE)
        if utc_offset:
            sign = 1 if utc_offset.group("sign") == "+" else -1
            hours = int(utc_offset.group("hours"))
            minutes = int(utc_offset.group("minutes") or "0")
            return ZoneInfo(f"Etc/GMT{'-' if sign > 0 else '+'}{hours}") if minutes == 0 else None
        try:
            return ZoneInfo(candidate)
        except Exception:
            english_alias = cls._TIMEZONE_ALIASES.get(normalized.lower().replace(" time", ""))
            if english_alias:
                try:
                    return ZoneInfo(english_alias)
                except Exception:
                    return None
        return None

    @staticmethod
    def _normalize_timezone_alias_key(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[，,。.!?？；;：:\s]+", "", text)
        for suffix in ("timezone", "time", "date", "日期", "时间", "时区", "现在", "当前", "市"):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
        return text.strip()

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _format_time_response(*, mode: str, now: datetime, label: str) -> str:
        weekday_map = {
            0: "星期一",
            1: "星期二",
            2: "星期三",
            3: "星期四",
            4: "星期五",
            5: "星期六",
            6: "星期日",
        }
        if mode == "date":
            return f"{label}当前日期：{now:%Y-%m-%d}"
        if mode == "weekday":
            return f"{label}今天是：{weekday_map[now.weekday()]}"
        return f"{label}当前时间：{now:%Y-%m-%d %H:%M:%S %Z}"

    @staticmethod
    def _weather_desc_from_day(day: dict[str, Any]) -> str:
        hourly = day.get("hourly")
        if isinstance(hourly, list) and hourly:
            preferred_indexes = (4, 3, 5, len(hourly) - 1)
            for index in preferred_indexes:
                if index < 0 or index >= len(hourly):
                    continue
                slot = hourly[index] if isinstance(hourly[index], dict) else {}
                desc_rows = slot.get("weatherDesc")
                if isinstance(desc_rows, list) and desc_rows:
                    first = desc_rows[0]
                    if isinstance(first, dict):
                        value = str(first.get("value") or "").strip()
                        if value:
                            return value
        return ""

    @classmethod
    def _extract_weather_payload(cls, tool_content: str) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for obj in cls._walk_json_candidates(tool_content):
            if isinstance(obj, dict):
                candidates.append(obj)
                text = obj.get("text")
                if isinstance(text, str):
                    for nested in cls._walk_json_candidates(text):
                        if isinstance(nested, dict):
                            candidates.append(nested)

        for candidate in candidates:
            weather = candidate.get("weather")
            if isinstance(weather, list) and weather:
                return candidate
        return None

    @staticmethod
    def _build_wttr_weather_lines(weather_payload: dict[str, Any], *, days: int) -> list[str]:
        forecast = weather_payload.get("weather")
        if not isinstance(forecast, list) or not forecast:
            return []

        lines: list[str] = []
        for day in forecast[:days]:
            if not isinstance(day, dict):
                continue
            date = str(day.get("date") or "").strip()
            if not date:
                continue
            desc = IntentRouter._weather_desc_from_day(day)
            high = str(day.get("maxtempC") or "").strip()
            low = str(day.get("mintempC") or "").strip()
            parts = [date]
            if desc:
                parts.append(desc)
            if high or low:
                parts.append(f"{low}~{high}°C" if high and low else f"{high or low}°C")
            lines.append(" ".join(parts))
        return lines

    @classmethod
    def _walk_json_candidates(cls, raw: str) -> list[Any]:
        out: list[Any] = []
        text = str(raw or "").strip()
        if not text:
            return out
        parsed = cls._safe_json_loads(text)
        if parsed is not None:
            out.append(parsed)

        decoder = json.JSONDecoder()
        for start in range(len(text)):
            if text[start] not in "{[":
                continue
            try:
                obj, _ = decoder.raw_decode(text[start:])
            except Exception:
                continue
            out.append(obj)
        return out

    @staticmethod
    def _safe_json_loads(text: str) -> Any | None:
        try:
            return json.loads(text)
        except Exception:
            return None

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
            return []

        geo_payload = self._extract_json_object(geo_result.content)
        results = geo_payload.get("results")
        if not isinstance(results, list) or not results:
            return []
        first = results[0] if isinstance(results[0], dict) else {}
        latitude = first.get("latitude")
        longitude = first.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            return []
        timezone = str(first.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"

        forecast_result = await self._fetch_with_retry(
            tools=tools,
            params={
                "url": (
                    "https://api.open-meteo.com/v1/forecast"
                    f"?latitude={latitude}&longitude={longitude}"
                    "&daily=weather_code,temperature_2m_max,temperature_2m_min"
                    f"&forecast_days={days}&timezone={quote(timezone)}"
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

    async def _fetch_with_retry(
        self,
        *,
        tools: ToolRegistry,
        params: dict[str, Any],
        trace_id: str,
    ):
        return await self._execute_with_retry(
            tools=tools,
            params=params,
            trace_id=trace_id,
            policy=self._LOW_RISK_FETCH_RETRY,
        )

    async def _execute_with_retry(
        self,
        *,
        tools: ToolRegistry,
        params: dict[str, Any],
        trace_id: str,
        policy: RetryPolicy,
    ):
        attempts = max(1, int(policy.max_attempts))
        last_result = None
        for _ in range(attempts):
            last_result = await tools.execute("web_fetch", params, trace_id=trace_id)
            if last_result.ok:
                return last_result
            if not bool(last_result.error and last_result.error.retryable):
                return last_result
        return last_result

    async def _run_source_fallback(
        self,
        sources: list[tuple[str, Any]],
    ) -> SourceFallbackResult:
        attempts: list[str] = []
        for source_name, source_loader in sources:
            attempts.append(source_name)
            value = await source_loader()
            if value is None:
                continue
            if isinstance(value, list) and not value:
                continue
            return SourceFallbackResult(value=value, winner=source_name, attempts=attempts)
        return SourceFallbackResult(value=None, winner=None, attempts=attempts)

    async def _fetch_exchange_rate_primary(
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
        return float(value)

    async def _fetch_exchange_rate_fallback(
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

    @classmethod
    def _extract_json_object(cls, tool_content: str) -> dict[str, Any]:
        for obj in cls._walk_json_candidates(tool_content):
            if isinstance(obj, dict):
                if isinstance(obj.get("text"), str):
                    nested = cls._safe_json_loads(obj["text"])
                    if isinstance(nested, dict):
                        return nested
                return obj
        return {}

    @staticmethod
    def _format_temperature(value: Any) -> str:
        if isinstance(value, int | float):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return f"{value:g}"
        text = str(value or "").strip()
        return text

    @staticmethod
    def _open_meteo_weather_desc(code: Any) -> str:
        code_int = int(code) if isinstance(code, int | float) or str(code).isdigit() else None
        mapping = {
            0: "晴",
            1: "大部晴朗",
            2: "多云",
            3: "阴",
            45: "雾",
            48: "冻雾",
            51: "小毛雨",
            53: "毛雨",
            55: "大毛雨",
            56: "小冻雨",
            57: "冻雨",
            61: "小雨",
            63: "中雨",
            65: "大雨",
            66: "小冻雨",
            67: "大冻雨",
            71: "小雪",
            73: "中雪",
            75: "大雪",
            77: "冰粒",
            80: "阵雨",
            81: "强阵雨",
            82: "暴雨",
            85: "阵雪",
            86: "强阵雪",
            95: "雷暴",
            96: "雷暴伴冰雹",
            99: "强雷暴伴冰雹",
        }
        if code_int is None:
            return ""
        return mapping.get(code_int, f"天气代码{code_int}")

    @staticmethod
    def _build_weather_failure_message(location: str) -> str:
        return (
            f"暂时无法获取{location}的天气数据。主天气源和备用天气源都未成功响应，可能是网络波动或上游服务异常，"
            "不是权限或审批问题。请稍后重试。"
        )

    @staticmethod
    def _fixed_site_language_order(topic: str) -> list[str]:
        if re.search(r"[\u4e00-\u9fff]", topic):
            return ["zh", "en"]
        return ["en", "zh"]

    async def _fetch_wikipedia_summary(
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
                    f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{quote(topic)}"
                ),
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
                return {"site": "wikipedia", "language": language, "title": title, "extract": extract}
        return await self._fetch_wikipedia_summary_via_query_api(
            language=language,
            topic=topic,
            tools=tools,
            trace_id=trace_id,
        )

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
        return {"site": "wikipedia", "language": language, "title": title, "extract": extract}

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

    @staticmethod
    def _build_fixed_site_failure_message(*, site: str, topic: str) -> str:
        site_label = "维基百科" if site == "wikipedia" else site
        return (
            f"暂时无法从{site_label}获取“{topic}”的摘要。主站点和备用站点都未成功返回可用内容，"
            "可能是网络波动、词条不存在或上游服务异常，不是权限或审批问题。请稍后重试或换个更明确的词条名。"
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

    @classmethod
    def _build_exchange_failure_message(cls, source: str, target: str) -> str:
        return (
            f"暂时无法获取{source}->{target}的汇率数据。主汇率源和备用汇率源都未成功响应，"
            "可能是网络波动或上游服务异常，不是权限或审批问题。请稍后重试。"
        )

    @staticmethod
    def _format_number(value: float, *, precision: int = 2) -> str:
        text = f"{value:.{precision}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    @classmethod
    def _build_weather_days_limit_message(
        cls,
        location: str,
        *,
        requested_days: int,
        max_supported_days: int,
    ) -> str:
        return (
            f"当前内置天气数据源最多支持未来{max_supported_days}天天气预报，"
            f"暂时无法直接提供{location}最近{requested_days}天的天气。"
            f"如果你愿意，我可以先给你最近{max_supported_days}天的真实天气，"
            f"或者继续按季节趋势补一份标注为估算的{requested_days}天天气趋势版。"
        )
