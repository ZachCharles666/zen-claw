"""Parsing and normalization helpers for intent router inputs."""

from __future__ import annotations

import re
from calendar import SUNDAY, monthrange
from datetime import UTC, datetime, timedelta, timezone
from difflib import get_close_matches
from zoneinfo import ZoneInfo


class IntentRouterParsersMixin:
    def _extract_weather_request(self, content: str) -> dict[str, str] | None:
        location = self._extract_weather_location(content)
        if not location:
            return None
        return {"content": content, "location": location}

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
                r"^(?:(?:请)?(?:告诉我|帮我|麻烦你|我想知道|我想看|想知道))?(?:查一下|查下|查查|看看|查询)?",
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

    @staticmethod
    def _should_route_recent_weather_to_history(content: str) -> bool:
        text = str(content or "").strip().lower()
        if not text:
            return False
        recent_tokens = ("最近", "过去", "近", "last", "past", "recent")
        future_tokens = ("未来", "接下来", "后面", "forecast", "预报", "将来")
        has_recent = any(token in text for token in recent_tokens)
        has_future = any(token in text for token in future_tokens)
        return has_recent and not has_future

    @classmethod
    def _extract_exchange_request(cls, content: str) -> dict[str, object] | None:
        text = str(content or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if not any(token in lowered for token in ("汇率", "兑换", "换成", "换算", "rate", "exchange", "兑")):
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
            topic = str(match.group("topic") or "").strip(" ：:，,。.？?！!()[]")
            if topic:
                return {"site": "wikipedia", "topic": topic}
        return None

    @staticmethod
    def _extract_exec_request(content: str) -> dict[str, str] | None:
        text = str(content or "").strip()
        if not text:
            return None
        patterns = (
            r"^(?:run|execute|exec)\s+(?:command|cmd|shell)\s+(?P<command>.+)$",
            r"^(?:运行|执行)\s*(?:命令|command|cmd|shell)\s*(?P<command>.+)$",
            r"^/exec\s+(?P<command>.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            command = str(match.group("command") or "").strip()
            if command:
                return {"command": command}
        return None

    @classmethod
    def _extract_time_request(cls, content: str) -> dict[str, str | None] | None:
        text = str(content or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if not any(token in lowered for token in ("几点", "几号", "星期几", "time", "date", "weekday", "时间", "日期")):
            return None
        mode = "time"
        if any(token in lowered for token in ("几号", "date", "日期")):
            mode = "date"
        elif any(token in lowered for token in ("星期几", "weekday")):
            mode = "weekday"
        label = cls._extract_time_timezone_label(text)
        timezone_key = cls._normalize_timezone_alias_key(label) if label else None
        return {"mode": mode, "timezone": timezone_key, "label": label}

    @classmethod
    def _extract_time_timezone_label(cls, text: str) -> str | None:
        patterns = (
            r"(?P<label>[\u4e00-\u9fffA-Za-z/_ -]{1,40}?)(?:现在|当前)?(?:几点|时间|日期|星期几)",
            r"(?:time|date|weekday)(?:\s+(?:in|for))?\s+(?P<label>[A-Za-z/_ -]{1,40})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            label = str(match.group("label") or "").strip()
            label = re.sub(r"^(请告诉我|告诉我|帮我|请|查询|查一下|查下|我想知道)", "", label).strip()
            label = re.sub(r"(现在|当前)$", "", label).strip()
            if label in {"", "现在", "当前"}:
                continue
            if label and label.lower() not in {"what", "the", "is"}:
                return label
        return None

    @classmethod
    def _resolve_timezone_candidate(cls, value: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = cls._normalize_timezone_alias_key(text)
        if not normalized:
            return None
        alias = cls._TIMEZONE_ALIASES.get(normalized)
        if alias:
            return alias
        if "/" in text and len(text) <= 64:
            return text
        return None

    @classmethod
    def _fuzzy_timezone_alias_lookup(cls, value: str) -> str | None:
        normalized = cls._normalize_timezone_alias_key(value)
        if not normalized or normalized in cls._TIMEZONE_ALIASES:
            return None
        matches = get_close_matches(normalized, cls._TIMEZONE_ALIASES.keys(), n=1, cutoff=0.82)
        return matches[0] if matches else None

    @classmethod
    def _resolve_timezone(cls, value: str) -> ZoneInfo | None:
        from zen_claw.agent import intent_router as intent_router_module

        zoneinfo_ctor = getattr(intent_router_module, "ZoneInfo", ZoneInfo)
        candidate = cls._resolve_timezone_candidate(value)
        if candidate is None:
            fuzzy_match = cls._fuzzy_timezone_alias_lookup(value)
            if fuzzy_match is not None:
                candidate = cls._TIMEZONE_ALIASES.get(fuzzy_match)
        if candidate is None:
            return None
        try:
            return zoneinfo_ctor(candidate)
        except Exception:
            english_alias = re.sub(r"\s+", " ", str(value or "").strip()).lower()
            if english_alias:
                english_candidate = cls._TIMEZONE_ALIASES.get(english_alias)
                if english_candidate:
                    try:
                        return zoneinfo_ctor(english_candidate)
                    except Exception:
                        return None
        return None

    @classmethod
    def _fallback_now_in_timezone(cls, utc_now: datetime, candidate: str) -> datetime | None:
        if not isinstance(utc_now, datetime):
            return None
        now_utc = utc_now.astimezone(UTC)
        if candidate in cls._FALLBACK_FIXED_TIMEZONES:
            offset_hours, abbr = cls._FALLBACK_FIXED_TIMEZONES[candidate]
            tz = timezone(timedelta(hours=offset_hours), name=abbr)
            return now_utc.astimezone(tz)
        offset_hours, abbr = cls._fallback_dynamic_timezone(now_utc, candidate)
        if offset_hours is None or abbr is None:
            return None
        return now_utc.astimezone(timezone(timedelta(hours=offset_hours), name=abbr))

    @classmethod
    def _fallback_dynamic_timezone(
        cls, now_utc: datetime, candidate: str
    ) -> tuple[int | None, str | None]:
        year = now_utc.year
        if candidate == "America/New_York":
            dst = cls._is_between_utc(
                now_utc,
                cls._nth_weekday_utc(year, 3, SUNDAY, 2, 7),
                cls._nth_weekday_utc(year, 11, SUNDAY, 1, 6),
            )
            return (-4, "EDT") if dst else (-5, "EST")
        if candidate == "America/Chicago":
            dst = cls._is_between_utc(
                now_utc,
                cls._nth_weekday_utc(year, 3, SUNDAY, 2, 8),
                cls._nth_weekday_utc(year, 11, SUNDAY, 1, 7),
            )
            return (-5, "CDT") if dst else (-6, "CST")
        if candidate == "America/Los_Angeles":
            dst = cls._is_between_utc(
                now_utc,
                cls._nth_weekday_utc(year, 3, SUNDAY, 2, 10),
                cls._nth_weekday_utc(year, 11, SUNDAY, 1, 9),
            )
            return (-7, "PDT") if dst else (-8, "PST")
        if candidate == "Europe/London":
            dst = cls._is_between_utc(
                now_utc,
                cls._last_weekday_utc(year, 3, SUNDAY, 1),
                cls._last_weekday_utc(year, 10, SUNDAY, 1),
            )
            return (1, "BST") if dst else (0, "GMT")
        if candidate == "Europe/Paris":
            dst = cls._is_between_utc(
                now_utc,
                cls._last_weekday_utc(year, 3, SUNDAY, 1),
                cls._last_weekday_utc(year, 10, SUNDAY, 1),
            )
            return (2, "CEST") if dst else (1, "CET")
        if candidate == "Europe/Berlin":
            dst = cls._is_between_utc(
                now_utc,
                cls._last_weekday_utc(year, 3, SUNDAY, 1),
                cls._last_weekday_utc(year, 10, SUNDAY, 1),
            )
            return (2, "CEST") if dst else (1, "CET")
        if candidate == "Australia/Sydney":
            start = cls._nth_weekday_utc(year, 10, SUNDAY, 1, 16, day_offset=-1)
            end = cls._nth_weekday_utc(year, 4, SUNDAY, 1, 16, day_offset=-1)
            dst = now_utc >= start or now_utc < end
            return (11, "AEDT") if dst else (10, "AEST")
        return (None, None)

    @staticmethod
    def _is_between_utc(now_utc: datetime, start_utc: datetime, end_utc: datetime) -> bool:
        return start_utc <= now_utc < end_utc

    @staticmethod
    def _nth_weekday_utc(
        year: int,
        month: int,
        weekday: int,
        occurrence: int,
        hour_utc: int,
        *,
        day_offset: int = 0,
    ) -> datetime:
        day = 1
        hits = 0
        while True:
            current = datetime(year, month, day, tzinfo=UTC)
            if current.weekday() == weekday:
                hits += 1
                if hits == occurrence:
                    return current.replace(hour=hour_utc) + timedelta(days=day_offset)
            day += 1

    @staticmethod
    def _last_weekday_utc(year: int, month: int, weekday: int, hour_utc: int) -> datetime:
        last_day = monthrange(year, month)[1]
        for day in range(last_day, 0, -1):
            current = datetime(year, month, day, tzinfo=UTC)
            if current.weekday() == weekday:
                return current.replace(hour=hour_utc)
        raise ValueError("failed to locate weekday")

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
    def _match_direct_contract(content: str):
        from zen_claw.agent.direct_contracts import route as _route_direct

        return _route_direct(content)
