"""Tests for browser response-layer domain guard."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zen_claw.agent.tools._url_guard import is_domain_blocked
from zen_claw.agent.tools.browser import BrowserOpenTool
from zen_claw.agent.tools.result import ToolErrorKind


# ---------------------------------------------------------------------------
# _url_guard unit tests
# ---------------------------------------------------------------------------


def test_exact_domain_blocked():
    assert is_domain_blocked("https://evil.com/path", ["evil.com"]) is True


def test_subdomain_blocked():
    assert is_domain_blocked("https://sub.evil.com/path", ["evil.com"]) is True


def test_deep_subdomain_blocked():
    assert is_domain_blocked("https://a.b.evil.com/", ["evil.com"]) is True


def test_wildcard_pattern_blocked():
    assert is_domain_blocked("https://sub.evil.com/", ["*.evil.com"]) is True


def test_allowed_domain_not_blocked():
    assert is_domain_blocked("https://good.com/page", ["evil.com"]) is False


def test_partial_suffix_not_blocked():
    # "notevil.com" should NOT be blocked by "evil.com"
    assert is_domain_blocked("https://notevil.com/", ["evil.com"]) is False


def test_empty_blocked_list():
    assert is_domain_blocked("https://anything.com/", []) is False


def test_empty_url():
    assert is_domain_blocked("", ["evil.com"]) is False


def test_case_insensitive():
    assert is_domain_blocked("https://EVIL.COM/", ["evil.com"]) is True


# ---------------------------------------------------------------------------
# BrowserOpenTool response-layer integration tests
# ---------------------------------------------------------------------------


def _make_tool(blocked_domains: list[str]) -> BrowserOpenTool:
    return BrowserOpenTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4500/v1/browser",
        blocked_domains=blocked_domains,
    )


def _mock_response(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    return resp


@pytest.mark.asyncio
async def test_blocked_final_url_returns_error(monkeypatch):
    """If sidecar returns a finalUrl that matches blocked_domains, tool returns PERMISSION error."""
    tool = _make_tool(blocked_domains=["blocked.com"])

    sidecar_response = _mock_response({"ok": True, "finalUrl": "https://blocked.com/page"})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=sidecar_response)

    with patch("zen_claw.agent.tools.browser.httpx.AsyncClient", return_value=mock_client):
        result = await tool._execute_action({"url": "https://safe.com"}, trace_id="t1")

    assert not result.ok
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "browser_blocked_domain"
    assert "blocked.com" in result.error.message


@pytest.mark.asyncio
async def test_blocked_url_field_returns_error(monkeypatch):
    """Falls back to 'url' field when 'finalUrl' is absent."""
    tool = _make_tool(blocked_domains=["blocked.com"])

    sidecar_response = _mock_response({"ok": True, "url": "https://sub.blocked.com/"})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=sidecar_response)

    with patch("zen_claw.agent.tools.browser.httpx.AsyncClient", return_value=mock_client):
        result = await tool._execute_action({"url": "https://safe.com"}, trace_id="t2")

    assert not result.ok
    assert result.error.code == "browser_blocked_domain"


@pytest.mark.asyncio
async def test_allowed_final_url_passes_through(monkeypatch):
    """If finalUrl is not in blocked_domains, result is success."""
    tool = _make_tool(blocked_domains=["blocked.com"])

    payload = {"ok": True, "finalUrl": "https://allowed.com/page", "content": "hello"}
    sidecar_response = _mock_response(payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=sidecar_response)

    with patch("zen_claw.agent.tools.browser.httpx.AsyncClient", return_value=mock_client):
        result = await tool._execute_action({"url": "https://allowed.com/page"}, trace_id="t3")

    assert result.ok
    data = json.loads(result.content)
    assert data["finalUrl"] == "https://allowed.com/page"


@pytest.mark.asyncio
async def test_no_blocked_domains_no_check(monkeypatch):
    """When blocked_domains is empty, no response-layer check is performed."""
    tool = _make_tool(blocked_domains=[])

    payload = {"ok": True, "finalUrl": "https://anything.com/"}
    sidecar_response = _mock_response(payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=sidecar_response)

    with patch("zen_claw.agent.tools.browser.httpx.AsyncClient", return_value=mock_client):
        result = await tool._execute_action({"url": "https://anything.com/"}, trace_id="t4")

    assert result.ok


@pytest.mark.asyncio
async def test_no_url_in_response_skips_check(monkeypatch):
    """When response contains neither finalUrl nor url, no domain check is run."""
    tool = _make_tool(blocked_domains=["blocked.com"])

    payload = {"ok": True, "screenshot": "base64data"}
    sidecar_response = _mock_response(payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=sidecar_response)

    with patch("zen_claw.agent.tools.browser.httpx.AsyncClient", return_value=mock_client):
        result = await tool._execute_action({}, trace_id="t5")

    assert result.ok
