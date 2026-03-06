import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


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


class _ConstrainedWeatherProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if tools is None:
            raise AssertionError("Planning phase should be skipped for constrained weather replanning")
        if tools is not None:
            tool_names = [item["function"]["name"] for item in tools]
            assert tool_names == ["web_fetch"]
            if self.calls == 1:
                return LLMResponse(
                    content="retry weather via constrained tools",
                    tool_calls=[
                        ToolCallRequest(
                            id="weather-1",
                            name="web_fetch",
                            arguments={
                                "url": "https://wttr.in/%E6%88%90%E9%83%BD?format=j1",
                                "extractMode": "text",
                                "maxChars": 20000,
                            },
                        )
                    ],
                )
        return LLMResponse(content="2026-03-06 Sunny\n2026-03-07 Cloudy")

    def get_default_model(self) -> str:
        return "fake-model"


def test_process_direct_uses_constrained_replan_when_direct_route_fails(
    tmp_path: Path, monkeypatch
) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ConstrainedWeatherProvider(),
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
        payload = {
            "text": json.dumps(
                {
                    "weather": [
                        {
                            "date": "2026-03-06",
                            "maxtempC": "18",
                            "mintempC": "11",
                            "hourly": [{"weatherDesc": [{"value": "Sunny"}]}] * 8,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("告诉我成都最近一周的天气，我希望呈现方式是日期+天气的样式"))

    assert out == "2026-03-06 Sunny\n2026-03-07 Cloudy"
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
    assert calls["count"] == 2
