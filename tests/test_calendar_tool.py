"""Tests for CalendarTool (Google, Outlook, Apple backends)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zen_claw.agent.tools.calendar import CalendarTool, _fmt_event, _parse_dt
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.config.schema import (
    AppleCalendarConfig,
    CalendarConfig,
    GoogleCalendarConfig,
    OutlookCalendarConfig,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _google_config(enabled: bool = True) -> GoogleCalendarConfig:
    return GoogleCalendarConfig(
        enabled=enabled,
        token_file="~/.zen-claw/google_token.json",
        credentials_file="~/.zen-claw/google_credentials.json",
    )


def _outlook_config(enabled: bool = True) -> OutlookCalendarConfig:
    return OutlookCalendarConfig(
        enabled=enabled,
        client_id="test-client-id",
        tenant_id="common",
        token_cache_file="~/.zen-claw/outlook_token_cache.bin",
    )


def _apple_config(enabled: bool = True) -> AppleCalendarConfig:
    return AppleCalendarConfig(
        enabled=enabled,
        username="user@icloud.com",
        credential_platform="apple_calendar",
        credential_key="password",
    )


def _make_tool(google=False, outlook=False, apple=False) -> CalendarTool:
    cfg = CalendarConfig(
        google=_google_config(enabled=google),
        outlook=_outlook_config(enabled=outlook),
        apple=_apple_config(enabled=apple),
    )
    return CalendarTool(config=cfg)


def test_fmt_event_basic():
    s = _fmt_event("Meeting", "2026-03-27T09:00:00", "2026-03-27T10:00:00")
    assert "Meeting" in s
    assert "2026-03-27T09:00" in s


def test_parse_dt_with_value():
    dt = _parse_dt("2026-03-27T09:00:00Z")
    assert dt == "2026-03-27T09:00:00Z"


def test_parse_dt_none_returns_something():
    dt = _parse_dt(None)
    assert isinstance(dt, str)
    assert len(dt) > 0


# ── Config validation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_provider_enabled_returns_permission_error():
    tool = _make_tool()
    result = await tool.execute(action="list_events", provider="google")
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert "calendar_not_configured" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_unknown_provider_returns_parameter_error():
    tool = _make_tool(google=True)
    result = await tool.execute(action="list_events", provider="yahoo")
    assert not result.ok
    assert "calendar_provider_unavailable" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_google_disabled_but_asking_google():
    tool = _make_tool(outlook=True)
    result = await tool.execute(action="list_events", provider="google")
    assert not result.ok
    assert "calendar_provider_unavailable" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_create_event_missing_title():
    tool = _make_tool(google=True)
    result = await tool.execute(
        action="create_event",
        provider="google",
        start="2026-03-27T09:00:00Z",
        end="2026-03-27T10:00:00Z",
    )
    assert not result.ok
    assert "calendar_missing_title" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_update_event_missing_id():
    tool = _make_tool(google=True)
    result = await tool.execute(action="update_event", provider="google")
    assert not result.ok
    assert "calendar_missing_event_id" in ((result.error.code if result.error else "") or "")


@pytest.mark.asyncio
async def test_unknown_action():
    tool = _make_tool(google=True)
    result = await tool.execute(action="time_travel", provider="google")
    assert not result.ok
    assert "calendar_unknown_action" in ((result.error.code if result.error else "") or "")


# ── Google backend ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_google_list_events():
    tool = _make_tool(google=True)
    mock_svc = MagicMock()
    mock_svc.events().list().execute.return_value = {
        "items": [
            {
                "id": "ev-001",
                "summary": "Team Standup",
                "start": {"dateTime": "2026-03-27T09:00:00Z"},
                "end": {"dateTime": "2026-03-27T09:30:00Z"},
                "location": "Zoom",
            }
        ]
    }
    backend_mock = MagicMock()
    backend_mock.list_events.return_value = "2026-03-27T09:00 → 2026-03-27T09:30  Team Standup  @ Zoom"
    tool._backends["google"] = backend_mock

    result = await tool.execute(
        action="list_events",
        provider="google",
        start="2026-03-27T00:00:00Z",
        end="2026-03-27T23:59:59Z",
    )
    assert result.ok
    assert "Team Standup" in result.content


@pytest.mark.asyncio
async def test_google_create_event():
    tool = _make_tool(google=True)
    backend_mock = MagicMock()
    backend_mock.create_event.return_value = "Created event id=ev-new  'Sprint Review'"
    tool._backends["google"] = backend_mock

    result = await tool.execute(
        action="create_event",
        provider="google",
        title="Sprint Review",
        start="2026-03-27T14:00:00Z",
        end="2026-03-27T15:00:00Z",
        attendees=["alice@example.com", "bob@example.com"],
    )
    assert result.ok
    assert "Sprint Review" in result.content
    backend_mock.create_event.assert_called_once()
    # attendees list is passed as positional arg
    all_args = list(backend_mock.create_event.call_args.args) + list(backend_mock.create_event.call_args.kwargs.values())
    assert any("alice@example.com" in str(a) for a in all_args)


# ── Outlook backend ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outlook_list_events():
    tool = _make_tool(outlook=True)
    backend_mock = MagicMock()
    backend_mock.list_events.return_value = "2026-03-27T10:00 → 2026-03-27T11:00  1:1 with Manager"
    tool._backends["outlook"] = backend_mock

    result = await tool.execute(
        action="list_events",
        provider="outlook",
        start="2026-03-27T00:00:00Z",
        end="2026-03-27T23:59:59Z",
    )
    assert result.ok
    assert "1:1 with Manager" in result.content


@pytest.mark.asyncio
async def test_outlook_missing_token_returns_setup_error():
    tool = _make_tool(outlook=True)
    from zen_claw.agent.tools.calendar import _OutlookCalendarBackend

    backend = _OutlookCalendarBackend(tool._cfg.outlook)

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_app.acquire_token_silent.return_value = None

    mock_msal = MagicMock()
    mock_msal.PublicClientApplication.return_value = mock_app
    mock_msal.SerializableTokenCache.return_value = MagicMock(has_state_changed=False)
    with patch.dict("sys.modules", {"msal": mock_msal}):
        with pytest.raises(RuntimeError, match="zen-claw auth outlook"):
            backend._get_token()


# ── Apple backend ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apple_missing_password_returns_setup_error():
    tool = _make_tool(apple=True)
    from zen_claw.agent.tools.calendar import _AppleCalendarBackend

    backend = _AppleCalendarBackend(tool._cfg.apple)
    vault = MagicMock()
    vault.get.return_value = None
    backend._vault = vault

    mock_caldav = MagicMock()
    with patch.dict("sys.modules", {"caldav": mock_caldav}):
        with pytest.raises(RuntimeError, match="apple_calendar password"):
            backend._get_client()


@pytest.mark.asyncio
async def test_apple_missing_dep_returns_setup_error():
    tool = _make_tool(apple=True)
    from zen_claw.agent.tools.calendar import _AppleCalendarBackend

    backend = _AppleCalendarBackend(tool._cfg.apple)
    vault = MagicMock()
    vault.get.return_value = "app-specific-password"
    backend._vault = vault

    with patch.dict("sys.modules", {"caldav": None}):
        with pytest.raises(RuntimeError, match="caldav not installed"):
            backend._get_client()


# ── find_free_slots ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_free_slots_google():
    tool = _make_tool(google=True)
    backend_mock = MagicMock()
    backend_mock.find_free_slots.return_value = "You are free from 2026-03-27T09:00 to 2026-03-27T18:00."
    tool._backends["google"] = backend_mock

    result = await tool.execute(
        action="find_free_slots",
        provider="google",
        start="2026-03-27T09:00:00Z",
        end="2026-03-27T18:00:00Z",
    )
    assert result.ok
    assert "free" in result.content.lower()
