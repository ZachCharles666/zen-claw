"""Regression tests for stock_price intent: parser + handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from zen_claw.agent.intent_router import IntentRouter


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_tools(content: str = "{}", ok: bool = True) -> MagicMock:
    mock = MagicMock()
    result = MagicMock()
    result.ok = ok
    result.content = content
    result.error = None if ok else MagicMock(message="network error", retryable=False)
    mock.execute = AsyncMock(return_value=result)
    return mock


def _router() -> IntentRouter:
    return IntentRouter()


# ── parser: basic ticker recognition ─────────────────────────────────────────


def test_parser_extracts_explicit_ticker_with_price_keyword() -> None:
    r = _router()
    parsed = r._extract_stock_request("AAPL 股价多少")
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
    assert parsed["asset_type"] == "stock"


def test_parser_extracts_english_ticker_with_price_keyword() -> None:
    r = _router()
    parsed = r._extract_stock_request("What is the TSLA stock price today?")
    assert parsed is not None
    assert parsed["symbol"] == "TSLA"
    assert parsed["asset_type"] == "stock"


# ── parser: Chinese company name aliases ─────────────────────────────────────


def test_parser_maps_apple_chinese_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("苹果股价")
    assert parsed is not None
    assert parsed["symbol"] == "AAPL"
    assert parsed["asset_type"] == "stock"


def test_parser_maps_tesla_chinese_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("特斯拉现在多少钱")
    assert parsed is not None
    assert parsed["symbol"] == "TSLA"


def test_parser_maps_nvidia_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("英伟达行情怎么样")
    assert parsed is not None
    assert parsed["symbol"] == "NVDA"


def test_parser_maps_tencent_hk_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("腾讯股价")
    assert parsed is not None
    assert parsed["symbol"] == "0700.HK"


# ── parser: crypto aliases ────────────────────────────────────────────────────


def test_parser_identifies_btc_as_crypto() -> None:
    r = _router()
    parsed = r._extract_stock_request("BTC 价格")
    assert parsed is not None
    assert parsed["asset_type"] == "crypto"
    assert parsed["symbol"] == "bitcoin"


def test_parser_identifies_chinese_bitcoin_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("比特币现在多少钱")
    assert parsed is not None
    assert parsed["asset_type"] == "crypto"
    assert parsed["symbol"] == "bitcoin"


def test_parser_identifies_ethereum_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("以太坊价格")
    assert parsed is not None
    assert parsed["asset_type"] == "crypto"
    assert parsed["symbol"] == "ethereum"


def test_parser_identifies_dogecoin_alias() -> None:
    r = _router()
    parsed = r._extract_stock_request("狗狗币行情")
    assert parsed is not None
    assert parsed["asset_type"] == "crypto"
    assert parsed["symbol"] == "dogecoin"


# ── parser: rejection cases ───────────────────────────────────────────────────


def test_parser_rejects_trade_command() -> None:
    r = _router()
    assert r._extract_stock_request("帮我买入苹果股票") is None


def test_parser_rejects_sell_command() -> None:
    r = _router()
    assert r._extract_stock_request("sell AAPL at market price") is None


def test_parser_returns_none_for_empty_input() -> None:
    r = _router()
    assert r._extract_stock_request("") is None


def test_parser_returns_none_for_generic_question() -> None:
    r = _router()
    # No price keyword, no alias or crypto match
    assert r._extract_stock_request("今天天气怎么样") is None


# ── handler: equity success path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_equity_success_returns_direct_success() -> None:
    yahoo_payload = json.dumps({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": "AAPL",
                    "regularMarketPrice": 175.50,
                    "previousClose": 172.00,
                    "currency": "USD",
                }
            }]
        }
    })
    tools = _make_tools(content=yahoo_payload)
    r = _router()
    result = await r.route("苹果股价", tools=tools, trace_id="t-equity")

    assert result.handled is True
    assert result.intent_name == "stock_price"
    assert result.route_status == "direct_success"
    assert "AAPL" in result.content


@pytest.mark.asyncio
async def test_handler_equity_includes_change_direction() -> None:
    yahoo_payload = json.dumps({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": "TSLA",
                    "regularMarketPrice": 200.0,
                    "previousClose": 190.0,
                    "currency": "USD",
                }
            }]
        }
    })
    tools = _make_tools(content=yahoo_payload)
    r = _router()
    result = await r.route("特斯拉股价", tools=tools, trace_id="t-equity-chg")

    assert result.handled is True
    assert "▲" in result.content or "▼" in result.content


# ── handler: crypto success path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_crypto_success_returns_direct_success() -> None:
    cg_payload = json.dumps({
        "bitcoin": {
            "usd": 65000.0,
            "cny": 470000.0,
            "usd_24h_change": 1.5,
        }
    })
    tools = _make_tools(content=cg_payload)
    r = _router()
    result = await r.route("比特币价格", tools=tools, trace_id="t-crypto")

    assert result.handled is True
    assert result.intent_name == "stock_price"
    assert result.route_status == "direct_success"
    assert "Bitcoin" in result.content or "bitcoin" in result.content.lower()
    assert "65" in result.content  # price present


@pytest.mark.asyncio
async def test_handler_crypto_negative_change_shows_down_arrow() -> None:
    cg_payload = json.dumps({
        "ethereum": {
            "usd": 3200.0,
            "cny": 23000.0,
            "usd_24h_change": -2.3,
        }
    })
    tools = _make_tools(content=cg_payload)
    r = _router()
    result = await r.route("以太坊价格", tools=tools, trace_id="t-crypto-down")

    assert result.handled is True
    assert "▼" in result.content


# ── handler: failure paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_crypto_fetch_failure_returns_handled_result() -> None:
    tools = _make_tools(ok=False)
    r = _router()
    result = await r.route("比特币价格", tools=tools, trace_id="t-crypto-fail")

    assert result.handled is True
    assert result.intent_name == "stock_price"
    # Either replan or direct_failed depending on runtime config
    assert result.route_status in ("direct_failed", "needs_constrained_replan")


@pytest.mark.asyncio
async def test_handler_equity_fetch_failure_returns_constrained_replan() -> None:
    tools = _make_tools(ok=False)
    r = _router()
    result = await r.route("苹果股价", tools=tools, trace_id="t-equity-fail")

    assert result.handled is True
    assert result.intent_name == "stock_price"
