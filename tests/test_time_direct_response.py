import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
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
        raise AssertionError("LLM chat should not be called for direct time/date response")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_loop(tmp_path):
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


def test_process_direct_returns_current_time_without_llm(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 4, 5, 6, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("请告诉我现在几点"))

    assert "当前时区当前时间：" in out
    assert "2026-03-07" in out
    assert ":05:06" in out


def test_process_direct_returns_target_timezone_time_without_llm(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("请告诉我纽约现在几点"))

    assert out.startswith("纽约当前时间：")
    assert "2026-03-07 07:00:00 EST" in out


def test_process_direct_returns_target_timezone_date_without_llm(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("请告诉我东京现在日期"))

    assert out == "东京当前日期：2026-03-07"


def test_process_direct_accepts_city_alias_with_suffix_without_llm(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("请告诉我纽约市现在几点"))

    assert out.startswith("纽约市当前时间：")
    assert "2026-03-07 07:00:00 EST" in out


def test_process_direct_accepts_english_city_alias_without_llm(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("time in New York"))

    assert out.startswith("New York当前时间：")
    assert "2026-03-07 07:00:00 EST" in out


def test_process_direct_fuzzy_matches_slightly_misspelled_english_city_alias(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("time in Nwe York"))

    assert out.startswith("Nwe York当前时间：")
    assert "2026-03-07 07:00:00 EST" in out


def test_process_direct_falls_back_when_zoneinfo_database_is_unavailable_for_new_york(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 8, 10, 0, 0, tzinfo=UTC)),
    )
    def _raise_zoneinfo(*_args, **_kwargs):
        raise Exception("no tzdata")

    monkeypatch.setattr("zen_claw.agent.intent_router.ZoneInfo", _raise_zoneinfo)

    out = asyncio.run(loop.process_direct("请告诉我纽约现在几点"))

    assert out.startswith("纽约当前时间：")
    assert "2026-03-08 06:00:00 EDT" in out


def test_process_direct_falls_back_when_zoneinfo_database_is_unavailable_for_tokyo(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )
    def _raise_zoneinfo(*_args, **_kwargs):
        raise Exception("no tzdata")

    monkeypatch.setattr("zen_claw.agent.intent_router.ZoneInfo", _raise_zoneinfo)

    out = asyncio.run(loop.process_direct("请告诉我东京现在日期"))

    assert out == "东京当前日期：2026-03-07"


def test_process_direct_returns_deterministic_failure_for_unknown_timezone(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        loop.intent_router,
        "_utc_now",
        staticmethod(lambda: datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)),
    )

    out = asyncio.run(loop.process_direct("请告诉我火星基地现在几点"))

    assert "暂时无法识别" in out
    assert "当前卡点不是权限或审批问题" in out
    assert "火星基地" in out
    assert "缺的是可确认的城市、地区或标准时区名" in out
    assert "America/New_York" in out
