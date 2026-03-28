"""Shared fetch/retry/JSON helpers for the intent router."""

from __future__ import annotations

import json
from typing import Any

from zen_claw.agent.intent_router_contracts import RetryPolicy, SourceFallbackResult
from zen_claw.agent.tools.registry import ToolRegistry


class IntentRouterSharedMixin:
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
    def _build_wttr_weather_lines(cls, weather_payload: dict[str, Any], *, days: int) -> list[str]:
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
            desc = cls._weather_desc_from_day(day)
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
        return str(value or "").strip()

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
    def _format_number(value: float, *, precision: int = 2) -> str:
        text = f"{value:.{precision}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
