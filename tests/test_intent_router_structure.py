from __future__ import annotations

import pytest

from zen_claw.agent.intent_router import IntentRouter


def test_intent_registry_order_is_stable() -> None:
    router = IntentRouter()
    entries = router._iter_intent_registry()
    assert [entry.intent_name for entry in entries] == [
        "code_exec",
        "weather",
        "exchange_rate",
        "fixed_site_fetch",
        "time",
        "direct_contracts",
    ]
    assert [(entry.parser_name, entry.handler_name) for entry in entries] == [
        ("_extract_exec_request", "_handle_exec_request"),
        ("_extract_weather_request", "_handle_weather_request"),
        ("_extract_exchange_request", "_handle_exchange_request"),
        ("_extract_fixed_site_request", "_handle_fixed_site_request"),
        ("_extract_time_request", "_handle_time_request"),
        ("_match_direct_contract", "_handle_direct_contract_request"),
    ]


def test_weather_request_parser_wraps_content_and_location() -> None:
    router = IntentRouter()
    parsed = router._extract_weather_request("帮我查一下上海未来7天天气")
    assert parsed == {
        "content": "帮我查一下上海未来7天天气",
        "location": "上海",
    }


@pytest.mark.asyncio
async def test_run_source_fallback_returns_winner_and_attempts() -> None:
    router = IntentRouter()

    async def _none():
        return None

    async def _winner():
        return {"ok": True}

    result = await router._run_source_fallback(
        [
            ("primary", _none),
            ("fallback", _winner),
        ]
    )

    assert result.winner == "fallback"
    assert result.attempts == ["primary", "fallback"]
    assert result.value == {"ok": True}
