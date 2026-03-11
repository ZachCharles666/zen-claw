import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


class _FailIfCalledProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise AssertionError("LLM chat should not be called for direct weather response")

    def get_default_model(self) -> str:
        return "fake-model"


def test_process_direct_returns_weather_without_llm(tmp_path: Path, monkeypatch) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        assert "%E6%88%90%E9%83%BD" in params["url"]
        payload = {
            "text": json.dumps(
                {
                    "weather": [
                        {
                            "date": "2026-03-06",
                            "maxtempC": "18",
                            "mintempC": "11",
                            "hourly": [{"weatherDesc": [{"value": "Sunny"}]}] * 8,
                        },
                        {
                            "date": "2026-03-07",
                            "maxtempC": "17",
                            "mintempC": "10",
                            "hourly": [{"weatherDesc": [{"value": "Cloudy"}]}] * 8,
                        },
                        {
                            "date": "2026-03-08",
                            "maxtempC": "16",
                            "mintempC": "9",
                            "hourly": [{"weatherDesc": [{"value": "Rain"}]}] * 8,
                        },
                        {
                            "date": "2026-03-09",
                            "maxtempC": "15",
                            "mintempC": "8",
                            "hourly": [{"weatherDesc": [{"value": "Sunny"}]}] * 8,
                        },
                        {
                            "date": "2026-03-10",
                            "maxtempC": "14",
                            "mintempC": "7",
                            "hourly": [{"weatherDesc": [{"value": "Cloudy"}]}] * 8,
                        },
                        {
                            "date": "2026-03-11",
                            "maxtempC": "13",
                            "mintempC": "6",
                            "hourly": [{"weatherDesc": [{"value": "Rain"}]}] * 8,
                        },
                        {
                            "date": "2026-03-12",
                            "maxtempC": "12",
                            "mintempC": "5",
                            "hourly": [{"weatherDesc": [{"value": "Sunny"}]}] * 8,
                        },
                        {
                            "date": "2026-03-13",
                            "maxtempC": "11",
                            "mintempC": "4",
                            "hourly": [{"weatherDesc": [{"value": "Cloudy"}]}] * 8,
                        },
                    ]
                },
                ensure_ascii=False,
            )
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近一周的天气，我希望呈现方式是日期+天气的样式"))

    assert out.startswith("成都天气预报：")
    assert "2026-03-06 Sunny 11~18°C" in out
    assert "2026-03-07 Cloudy 10~17°C" in out
    assert "2026-03-12 Sunny 5~12°C" in out


def test_process_direct_returns_14_day_weather_without_llm(tmp_path: Path, monkeypatch) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        payload = {
            "text": json.dumps(
                {
                    "weather": [
                        {
                            "date": f"2026-03-{day:02d}",
                            "maxtempC": str(20 - (day % 5)),
                            "mintempC": str(10 - (day % 3)),
                            "hourly": [{"weatherDesc": [{"value": "Sunny"}]}] * 8,
                        }
                        for day in range(7, 21)
                    ]
                },
                ensure_ascii=False,
            )
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近14天的天气，需要给我的结果是日期+天气的样式"))

    assert out.startswith("成都天气预报：")
    assert out.count("\n") == 14
    assert "2026-03-20 Sunny" in out


def test_process_direct_returns_deterministic_failure_when_primary_payload_is_bad_and_fallback_fails(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    calls = {"count": 0}

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        calls["count"] += 1
        assert name == "web_fetch"
        if calls["count"] == 1:
            return ToolResult.success("not-json-response")
        return ToolResult.success("{}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近一周的天气，我希望呈现方式是日期+天气的样式"))

    assert "不是权限或审批问题" in out
    assert "暂时无法获取成都的天气数据" in out
    assert calls["count"] == 2


def test_process_direct_returns_deterministic_failure_when_weather_fetch_retries_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    calls = {"count": 0}

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        calls["count"] += 1
        assert name == "web_fetch"
        return ToolResult.failure(
            kind=ToolErrorKind.RETRYABLE,
            message="timed out",
            code="web_fetch_timeout",
        )

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近7天的天气，需要给我的结果是日期+天气的样式"))

    assert "不是权限或审批问题" in out
    assert "暂时无法获取成都的天气数据" in out
    assert "主天气源和备用天气源都未成功响应" in out
    assert calls["count"] == 4


def test_process_direct_falls_back_to_open_meteo_when_wttr_times_out(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        url = params["url"]
        calls.append(url)
        if "wttr.in" in url:
            return ToolResult.failure(
                kind=ToolErrorKind.RETRYABLE,
                message="timed out",
                code="web_fetch_timeout",
            )
        if "geocoding-api.open-meteo.com" in url:
            payload = {
                "results": [
                    {
                        "name": "成都市",
                        "latitude": 30.66667,
                        "longitude": 104.06667,
                        "timezone": "Asia/Shanghai",
                    }
                ]
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        if "api.open-meteo.com" in url:
            payload = {
                "daily": {
                    "time": ["2026-03-06", "2026-03-07"],
                    "weather_code": [1, 63],
                    "temperature_2m_max": [18.0, 17.0],
                    "temperature_2m_min": [11.0, 10.0],
                }
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近7天的天气，需要给我的结果是日期+天气的样式"))

    assert out.startswith("成都天气预报：")
    assert "2026-03-06 大部晴朗 11~18°C" in out
    assert "2026-03-07 中雨 10~17°C" in out
    assert len([url for url in calls if "wttr.in" in url]) == 2
    assert len([url for url in calls if "geocoding-api.open-meteo.com" in url]) == 1
    assert len([url for url in calls if "https://api.open-meteo.com/v1/forecast" in url]) == 1


def test_process_direct_falls_back_to_open_meteo_when_wttr_payload_is_unparseable(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        url = params["url"]
        calls.append(url)
        if "wttr.in" in url:
            assert params["maxChars"] == 80000
            return ToolResult.success('{"text":"{\\"weather\\": [')
        if "geocoding-api.open-meteo.com" in url:
            payload = {
                "results": [
                    {
                        "name": "成都市",
                        "latitude": 30.66667,
                        "longitude": 104.06667,
                        "timezone": "Asia/Shanghai",
                    }
                ]
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        if "api.open-meteo.com" in url:
            payload = {
                "daily": {
                    "time": ["2026-03-06", "2026-03-07"],
                    "weather_code": [0, 3],
                    "temperature_2m_max": [20.0, 18.0],
                    "temperature_2m_min": [12.0, 11.0],
                }
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近7天的天气，需要给我的结果是日期+天气的样式"))

    assert out.startswith("成都天气预报：")
    assert "2026-03-06 晴 12~20°C" in out
    assert "2026-03-07 阴 11~18°C" in out
    assert len([url for url in calls if "wttr.in" in url]) == 1
    assert len([url for url in calls if "geocoding-api.open-meteo.com" in url]) == 1
    assert len([url for url in calls if "https://api.open-meteo.com/v1/forecast" in url]) == 1


def test_process_direct_falls_back_to_open_meteo_when_wttr_only_returns_three_days(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        url = params["url"]
        calls.append(url)
        if "wttr.in" in url:
            payload = {
                "text": json.dumps(
                    {
                        "weather": [
                            {
                                "date": "2026-03-07",
                                "maxtempC": "16",
                                "mintempC": "14",
                                "hourly": [{"weatherDesc": [{"value": "Overcast"}]}] * 8,
                            },
                            {
                                "date": "2026-03-08",
                                "maxtempC": "14",
                                "mintempC": "12",
                                "hourly": [{"weatherDesc": [{"value": "Patchy rain nearby"}]}] * 8,
                            },
                            {
                                "date": "2026-03-09",
                                "maxtempC": "12",
                                "mintempC": "11",
                                "hourly": [{"weatherDesc": [{"value": "Patchy rain nearby"}]}] * 8,
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=False))
        if "geocoding-api.open-meteo.com" in url:
            payload = {
                "results": [
                    {
                        "name": "成都市",
                        "latitude": 30.66667,
                        "longitude": 104.06667,
                        "timezone": "Asia/Shanghai",
                    }
                ]
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        if "api.open-meteo.com" in url:
            payload = {
                "daily": {
                    "time": [
                        "2026-03-07",
                        "2026-03-08",
                        "2026-03-09",
                        "2026-03-10",
                        "2026-03-11",
                        "2026-03-12",
                        "2026-03-13",
                    ],
                    "weather_code": [3, 61, 61, 63, 2, 3, 1],
                    "temperature_2m_max": [16.0, 14.0, 12.0, 13.0, 15.0, 16.0, 17.0],
                    "temperature_2m_min": [14.0, 12.0, 11.0, 10.0, 9.0, 8.0, 9.0],
                }
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近7天的天气，需要给我的结果是日期+天气的样式"))

    assert out.startswith("成都天气预报：")
    assert out.count("\n") == 7
    assert "2026-03-13 大部晴朗 9~17°C" in out
    assert len([url for url in calls if "wttr.in" in url]) == 1
    assert len([url for url in calls if "geocoding-api.open-meteo.com" in url]) == 1
    assert len([url for url in calls if "https://api.open-meteo.com/v1/forecast" in url]) == 1


def test_process_direct_reports_weather_limit_when_requested_days_exceed_supported_range(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    async def _fail_execute(name: str, params: dict, trace_id: str | None = None):
        raise AssertionError("weather requests beyond supported range should fail before tool execution")

    monkeypatch.setattr(loop.tools, "execute", _fail_execute)

    out = asyncio.run(loop.process_direct("告诉我成都未来70天的天气，需要给我的结果是日期+天气的样式"))

    assert "最多支持未来16天天气预报" in out
    assert "暂时无法直接提供成都未来70天的天气" in out
    assert "当前卡点不是权限或审批问题" in out
    assert "缺的是超过16天的可信长周期天气数据" in out
    assert "天气路由" not in out
    assert "主天气源" not in out
    assert "先返回成都最近16天的真实天气" in out
    assert "标注为估算的70天天气趋势版" in out


def test_process_direct_routes_recent_long_range_weather_to_history_archive(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=True,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 8, 12, 0, 0, tzinfo=UTC)),
    )

    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        url = params["url"]
        calls.append(url)
        if "geocoding-api.open-meteo.com" in url:
            payload = {
                "results": [
                    {
                        "name": "成都市",
                        "latitude": 30.66667,
                        "longitude": 104.06667,
                        "timezone": "Asia/Shanghai",
                    }
                ]
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        if "archive-api.open-meteo.com" in url:
            payload = {
                "daily": {
                    "time": ["2026-01-29", "2026-01-30", "2026-01-31"],
                    "weather_code": [1, 63, 3],
                    "temperature_2m_max": [18.0, 16.0, 14.0],
                    "temperature_2m_min": [10.0, 9.0, 8.0],
                }
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近70天的天气，需要给我的结果是日期+天气的样式"))

    assert out.startswith("成都最近70天天气记录：")
    assert "2026-01-29 大部晴朗 10~18°C" in out
    assert "2026-01-30 中雨 9~16°C" in out
    assert any("archive-api.open-meteo.com" in url for url in calls)
