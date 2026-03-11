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
        raise AssertionError("LLM chat should not be called for direct fixed-site response")

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


def test_process_direct_returns_wikipedia_summary_without_llm(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        assert (
            params["url"]
            == "https://zh.wikipedia.org/api/rest_v1/page/summary/%E9%98%BF%E5%85%B0%C2%B7%E5%9B%BE%E7%81%B5"
        )
        payload = {
            "title": "阿兰·图灵",
            "extract": "阿兰·图灵是英国数学家、逻辑学家和计算机科学先驱。",
        }
        return ToolResult.success(json.dumps({"text": json.dumps(payload, ensure_ascii=False)}))

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("用维基百科介绍阿兰·图灵"))

    assert out.startswith("维基百科中文摘要（阿兰·图灵）：")
    assert "计算机科学先驱" in out


def test_process_direct_falls_back_to_secondary_wikipedia_site(tmp_path: Path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        calls.append(params["url"])
        if params["url"] == "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing":
            payload = {"title": "Alan Turing"}
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        if (
            params["url"]
            == "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
            "&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Turing"
        ):
            payload = {"query": {"pages": [{"title": "Alan Turing"}]}}
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        if (
            params["url"]
            == "https://en.wikipedia.org/w/api.php?action=query&list=search&srwhat=text&srlimit=1"
            "&format=json&formatversion=2&srsearch=Alan%20Turing"
        ):
            payload = {"query": {"search": [{"title": "Alan Turing"}]}}
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        if params["url"] == "https://zh.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing":
            payload = {
                "title": "Alan Turing",
                "extract": "Alan Turing was a British mathematician and computing pioneer.",
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        raise AssertionError(f"unexpected url: {params['url']}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("wiki Alan Turing"))

    assert out.startswith("维基百科中文摘要（Alan Turing）：")
    assert "computing pioneer" in out
    assert calls == [
        "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Turing",
        "https://en.wikipedia.org/w/api.php?action=query&list=search&srwhat=text&srlimit=1&format=json&formatversion=2&srsearch=Alan%20Turing",
        "https://zh.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
    ]


def test_process_direct_returns_deterministic_failure_when_wikipedia_sources_fail(
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

    out = asyncio.run(loop.process_direct("wiki Alan Turing"))

    assert "暂时无法从维基百科获取“Alan Turing”的摘要" in out
    assert "不是权限或审批问题" in out
    assert calls["count"] == 12


def test_process_direct_falls_back_to_wikipedia_query_api_when_summary_endpoint_fails(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        calls.append(params["url"])
        if params["url"] == "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing":
            return ToolResult.failure(
                kind=ToolErrorKind.RETRYABLE,
                message="timed out",
                code="web_fetch_timeout",
            )
        if (
            params["url"]
            == "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
            "&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Turing"
        ):
            payload = {
                "query": {
                    "pages": [
                        {
                            "title": "Alan Turing",
                            "extract": "Alan Turing was a British mathematician and computing pioneer.",
                        }
                    ]
                }
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        raise AssertionError(f"unexpected url: {params['url']}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("wiki Alan Turing"))

    assert out.startswith("维基百科英文摘要（Alan Turing）：")
    assert "computing pioneer" in out
    assert calls == [
        "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
        "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Turing",
    ]


def test_process_direct_falls_back_to_wikipedia_search_then_summary(
    tmp_path: Path, monkeypatch
) -> None:
    loop = _make_loop(tmp_path)
    calls: list[str] = []

    async def _fake_execute(name: str, params: dict, trace_id: str | None = None):
        assert name == "web_fetch"
        url = params["url"]
        calls.append(url)
        if url == "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Mathison%20Turring":
            return ToolResult.success(json.dumps({"text": json.dumps({"title": "Alan Mathison Turring"})}))
        if (
            url
            == "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
            "&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Mathison%20Turring"
        ):
            return ToolResult.success(json.dumps({"text": json.dumps({"query": {"pages": [{"title": "Alan Mathison Turring"}]}})}))
        if (
            url
            == "https://en.wikipedia.org/w/api.php?action=query&list=search&srwhat=text&srlimit=1"
            "&format=json&formatversion=2&srsearch=Alan%20Mathison%20Turring"
        ):
            payload = {"query": {"searchinfo": {"suggestion": "Alan Turing"}, "search": []}}
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        if url == "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing":
            payload = {
                "title": "Alan Turing",
                "extract": "Alan Turing was a British mathematician and computing pioneer.",
            }
            return ToolResult.success(json.dumps({"text": json.dumps(payload)}))
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(loop.tools, "execute", _fake_execute)

    out = asyncio.run(loop.process_direct("wiki Alan Mathison Turring"))

    assert out.startswith("维基百科英文摘要（Alan Turing）：")
    assert "computing pioneer" in out
    assert calls == [
        "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Mathison%20Turring",
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&redirects=1&format=json&formatversion=2&titles=Alan%20Mathison%20Turring",
        "https://en.wikipedia.org/w/api.php?action=query&list=search&srwhat=text&srlimit=1&format=json&formatversion=2&srsearch=Alan%20Mathison%20Turring",
        "https://en.wikipedia.org/api/rest_v1/page/summary/Alan%20Turing",
    ]


def test_normalize_wikipedia_title_candidate_title_cases_ascii_suggestion() -> None:
    from zen_claw.agent.intent_router import IntentRouter

    assert (
        IntentRouter._normalize_wikipedia_title_candidate("alan mathison turing")
        == "Alan Mathison Turing"
    )
