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
        result = await tools.execute(
            "web_fetch",
            {
                "url": f"https://wttr.in/{quote(location)}?format=j1",
                "extractMode": "text",
                "maxChars": 20000,
            },
            trace_id=trace_id,
        )
        if not result.ok:
            return IntentRouteResult(
                handled=False,
                intent_name="weather",
                contract=self._WEATHER_CONTRACT,
                route_status="needs_constrained_replan",
                diagnostic=f"web_fetch_failed:{result.error.code if result.error else 'unknown'}",
            )

        weather_payload = self._extract_weather_payload(result.content)
        if not isinstance(weather_payload, dict):
            return IntentRouteResult(
                handled=False,
                intent_name="weather",
                contract=self._WEATHER_CONTRACT,
                route_status="needs_constrained_replan",
                diagnostic="weather_payload_not_parseable",
            )

        forecast = weather_payload.get("weather")
        if not isinstance(forecast, list) or not forecast:
            return IntentRouteResult(
                handled=False,
                intent_name="weather",
                contract=self._WEATHER_CONTRACT,
                route_status="needs_constrained_replan",
                diagnostic="weather_array_missing",
            )

        days = self._extract_weather_days(content)
        lines: list[str] = []
        for day in forecast[:days]:
            if not isinstance(day, dict):
                continue
            date = str(day.get("date") or "").strip()
            if not date:
                continue
            desc = self._weather_desc_from_day(day)
            high = str(day.get("maxtempC") or "").strip()
            low = str(day.get("mintempC") or "").strip()
            parts = [date]
            if desc:
                parts.append(desc)
            if high or low:
                parts.append(f"{low}~{high}°C" if high and low else f"{high or low}°C")
            lines.append(" ".join(parts))

        if not lines:
            return IntentRouteResult(
                handled=False,
                intent_name="weather",
                contract=self._WEATHER_CONTRACT,
                route_status="needs_constrained_replan",
                diagnostic="weather_lines_empty",
            )

        return IntentRouteResult(
            handled=True,
            intent_name="weather",
            content=f"{location}天气预报：\n" + "\n".join(lines),
            contract=self._WEATHER_CONTRACT,
            route_status="direct_success",
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
