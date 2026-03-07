import asyncio
import json
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
        raise AssertionError("LLM chat should not be called for deferred retry direct path")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_loop(tmp_path: Path) -> AgentLoop:
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
    loop._deferred_retry_delay_sec = 0.0
    return loop


@pytest.mark.asyncio
async def test_weather_direct_failure_schedules_deferred_retry_followup(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    calls = {"count": 0}

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        calls["count"] += 1
        if calls["count"] <= 4:
            return ToolResult.failure(
                kind=ToolErrorKind.RETRYABLE,
                message="timed out",
                code="web_fetch_timeout",
            )
        payload = {
            "text": json.dumps(
                {
                    "weather": [
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
                    ]
                },
                ensure_ascii=False,
            )
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    immediate = await loop.process_direct(
        "告诉我成都最近3天的天气",
        channel="discord",
        chat_id="u1",
        session_key="discord:u1",
    )
    followup = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)

    assert "暂时无法获取成都的天气数据" in immediate
    assert "我会在后台再试一次" in immediate
    assert followup.channel == "discord"
    assert followup.chat_id == "u1"
    assert followup.metadata["deferred_retry"] is True
    assert followup.metadata["intent_name"] == "weather"
    assert followup.content.startswith("后台重试成功：成都天气预报：")
    assert "2026-03-07 Cloudy 10~17°C" in followup.content
    assert calls["count"] == 5


@pytest.mark.asyncio
async def test_weather_deferred_retry_failure_publishes_failure_followup(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    calls = {"count": 0}

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        calls["count"] += 1
        return ToolResult.failure(
            kind=ToolErrorKind.RETRYABLE,
            message="timed out",
            code="web_fetch_timeout",
        )

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    immediate = await loop.process_direct(
        "告诉我成都最近3天的天气",
        channel="discord",
        chat_id="u2",
        session_key="discord:u2",
    )
    followup = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)

    assert "暂时无法获取成都的天气数据" in immediate
    assert "我会在后台再试一次" in immediate
    assert followup.channel == "discord"
    assert followup.chat_id == "u2"
    assert followup.metadata["deferred_retry"] is True
    assert followup.metadata["followup_kind"] == "failed"
    assert followup.metadata["intent_name"] == "weather"
    assert followup.content.startswith("后台重试后仍未成功：暂时无法获取成都的天气数据")
    assert "不是权限或审批问题" in followup.content
    assert calls["count"] == 8
