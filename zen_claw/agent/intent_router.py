"""Intent router for high-certainty pre-LLM utility requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from zen_claw.agent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class IntentToolContract:
    """Runtime-enforced tool contract for a recognized intent."""

    intent_name: str
    preferred_tools: list[str]
    allowed_tools: set[str]
    denied_tools: set[str]
    allow_constrained_replan: bool = True
    allow_high_risk_escalation: bool = False
    response_mode: Literal["direct", "llm_assisted"] = "direct"


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


class IntentRouter:
    """Handle a narrow set of deterministic, low-risk intents before LLM planning."""

    _WEATHER_CONTRACT = IntentToolContract(
        intent_name="weather",
        preferred_tools=["web_fetch"],
        allowed_tools={"web_fetch"},
        denied_tools={"exec", "spawn", "write_file", "edit_file"},
        allow_constrained_replan=True,
        allow_high_risk_escalation=False,
        response_mode="direct",
    )

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
        return IntentRouteResult(handled=False)

    async def _route_weather(
        self,
        content: str,
        *,
        location: str,
        tools: ToolRegistry,
        trace_id: str,
    ) -> IntentRouteResult:
        days = self._extract_weather_days(content)

        wttr_result = await self._fetch_weather_payload_text(
            location=location,
            tools=tools,
            trace_id=trace_id,
        )
        if wttr_result.ok:
            weather_payload = self._extract_weather_payload(wttr_result.content)
            if isinstance(weather_payload, dict):
                lines = self._build_wttr_weather_lines(weather_payload, days=days)
                if lines:
                    return IntentRouteResult(
                        handled=True,
                        intent_name="weather",
                        content=f"{location}天气预报：\n" + "\n".join(lines),
                        contract=self._WEATHER_CONTRACT,
                        route_status="direct_success",
                    )
            fallback_lines = await self._fetch_open_meteo_weather_lines(
                location=location,
                days=days,
                tools=tools,
                trace_id=trace_id,
            )
            if fallback_lines:
                return IntentRouteResult(
                    handled=True,
                    intent_name="weather",
                    content=f"{location}天气预报：\n" + "\n".join(fallback_lines),
                    contract=self._WEATHER_CONTRACT,
                    route_status="direct_success",
                )
            return IntentRouteResult(
                handled=True,
                intent_name="weather",
                content=self._build_weather_failure_message(location),
                contract=self._WEATHER_CONTRACT,
                route_status="direct_failed",
                diagnostic="weather_payload_not_parseable",
            )

        fallback_lines = await self._fetch_open_meteo_weather_lines(
            location=location,
            days=days,
            tools=tools,
            trace_id=trace_id,
        )
        if fallback_lines:
            return IntentRouteResult(
                handled=True,
                intent_name="weather",
                content=f"{location}天气预报：\n" + "\n".join(fallback_lines),
                contract=self._WEATHER_CONTRACT,
                route_status="direct_success",
            )

        if not wttr_result.ok:
            return IntentRouteResult(
                handled=True,
                intent_name="weather",
                content=self._build_weather_failure_message(location),
                contract=self._WEATHER_CONTRACT,
                route_status="direct_failed",
                diagnostic=f"weather_sources_failed:{wttr_result.error.code if wttr_result.error else 'unknown'}",
            )

        return IntentRouteResult(
            handled=False,
            intent_name="weather",
            contract=self._WEATHER_CONTRACT,
            route_status="needs_constrained_replan",
            diagnostic="weather_lines_empty",
            skip_planning=True,
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
            r"(?P<loc>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-zA-Za-z\s·\-.]{0,40}?)(?:最近一周|未来一周|这一周|本周|近7天|未来7天|7天|今天天气|今日天气|今天|今日)?的?天气",
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
                r"(最近一周|未来一周|这一周|本周|近7天|未来7天|最近7天|7天|一周|最近|未来|最)$",
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
        if any(token in text for token in ("最近一周", "未来一周", "这一周", "本周", "7天", "7-day", "week")):
            return 7
        return 3

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
        result = await tools.execute("web_fetch", params, trace_id=trace_id)
        if result.ok:
            return result
        retryable = bool(result.error and result.error.retryable)
        if not retryable:
            return result
        return await tools.execute("web_fetch", params, trace_id=trace_id)

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
        result = await tools.execute("web_fetch", params, trace_id=trace_id)
        if result.ok:
            return result
        if not bool(result.error and result.error.retryable):
            return result
        return await tools.execute("web_fetch", params, trace_id=trace_id)

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
