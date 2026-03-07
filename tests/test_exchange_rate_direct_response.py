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
        raise AssertionError("LLM chat should not be called for direct exchange-rate response")

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
    return loop


def test_process_direct_returns_exchange_rate_without_llm(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        assert params["url"] == "https://open.er-api.com/v6/latest/USD"
        payload = {"result": "success", "base_code": "USD", "rates": {"CNY": 7.23}}
        return ToolResult.success(json.dumps({"text": json.dumps(payload)}))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("100美元兑换人民币汇率是多少"))

    assert out == "100美元 ≈ 723人民币。参考汇率：1 USD = 7.23 CNY。"


def test_process_direct_falls_back_to_secondary_exchange_source(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        calls.append(params["url"])
        if "open.er-api.com" in params["url"]:
            return ToolResult.failure(
                kind=ToolErrorKind.RETRYABLE,
                message="timed out",
                code="web_fetch_timeout",
            )
        if "api.frankfurter.app" in params["url"]:
            payload = {"amount": 1.0, "base": "EUR", "rates": {"JPY": 161.5}}
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        raise AssertionError(f"unexpected url: {params['url']}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("欧元兑日元汇率是多少"))

    assert out == "1欧元 ≈ 161.5日元。参考汇率：1 EUR = 161.5 JPY。"
    assert len([url for url in calls if "open.er-api.com" in url]) == 2
    assert len([url for url in calls if "api.frankfurter.app" in url]) == 1


def test_process_direct_returns_deterministic_failure_when_exchange_sources_fail(
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

    out = asyncio.run(loop.process_direct("美元兑人民币汇率是多少"))

    assert "暂时无法获取USD->CNY的汇率数据" in out
    assert "不是权限或审批问题" in out
    assert calls["count"] == 4
