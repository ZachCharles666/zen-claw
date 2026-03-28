"""Calendar tool — Google Calendar, Outlook (MSAL + Graph API), Apple CalDAV."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

if TYPE_CHECKING:
    from zen_claw.auth.credentials import CredentialVault
    from zen_claw.config.schema import CalendarConfig

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _parse_dt(s: str | None) -> str:
    """Return ISO datetime string, defaulting to now."""
    if not s:
        return datetime.now(timezone.utc).isoformat()
    return s


def _fmt_event(title: str, start: str, end: str, location: str = "", eid: str = "") -> str:
    loc = f"  @ {location}" if location else ""
    id_part = f"  id={eid}" if eid else ""
    return f"{start[:16]} → {end[:16]}  {title}{loc}{id_part}"


# ── Google Calendar backend ────────────────────────────────────────────────────


class _GoogleCalendarBackend:
    def __init__(self, config: Any):
        self._cfg = config
        self._service: Any = None

    def _get_service(self) -> Any:
        if self._service is None:
            from zen_claw.agent.tools.gdrive import _load_google_credentials

            creds = _load_google_credentials(
                self._cfg.token_file,
                self._cfg.credentials_file,
                self._cfg.scopes,
            )
            try:
                from googleapiclient.discovery import build  # type: ignore[import-untyped]
            except ImportError:
                raise RuntimeError(
                    "google-api-python-client not installed. Run: pip install 'zen-claw-ai[calendar]'"
                )
            self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def list_events(self, start: str, end: str, tz: str) -> str:
        svc = self._get_service()
        resp = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=start,
                timeMax=end,
                singleEvents=True,
                orderBy="startTime",
                timeZone=tz or "UTC",
                maxResults=50,
            )
            .execute()
        )
        items = resp.get("items", [])
        if not items:
            return "No events in the specified range."
        lines = []
        for ev in items:
            title = ev.get("summary", "(no title)")
            s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
            e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date", "")
            loc = ev.get("location", "")
            lines.append(_fmt_event(title, s, e, loc, ev.get("id", "")))
        return "\n".join(lines)

    def create_event(self, title: str, start: str, end: str, description: str, attendees: list[str], tz: str) -> str:
        svc = self._get_service()
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": tz or "UTC"},
            "end": {"dateTime": end, "timeZone": tz or "UTC"},
        }
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        ev = svc.events().insert(calendarId="primary", body=body).execute()
        return f"Created event id={ev['id']}  {title!r}"

    def update_event(self, event_id: str, title: str | None, start: str | None, end: str | None, tz: str) -> str:
        svc = self._get_service()
        ev = svc.events().get(calendarId="primary", eventId=event_id).execute()
        if title:
            ev["summary"] = title
        if start:
            ev["start"] = {"dateTime": start, "timeZone": tz or "UTC"}
        if end:
            ev["end"] = {"dateTime": end, "timeZone": tz or "UTC"}
        updated = svc.events().update(calendarId="primary", eventId=event_id, body=ev).execute()
        return f"Updated event id={updated['id']}"

    def delete_event(self, event_id: str) -> str:
        svc = self._get_service()
        svc.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"Deleted event id={event_id}"

    def find_free_slots(self, start: str, end: str, tz: str) -> str:
        svc = self._get_service()
        body = {
            "timeMin": start,
            "timeMax": end,
            "timeZone": tz or "UTC",
            "items": [{"id": "primary"}],
        }
        resp = svc.freebusy().query(body=body).execute()
        busy = resp.get("calendars", {}).get("primary", {}).get("busy", [])
        if not busy:
            return f"You are free from {start[:16]} to {end[:16]}."
        lines = [f"Busy: {b['start'][:16]} → {b['end'][:16]}" for b in busy]
        return "\n".join(lines)


# ── Outlook Calendar backend (MSAL + Microsoft Graph) ─────────────────────────


class _OutlookCalendarBackend:
    _GRAPH_BASE = "https://graph.microsoft.com/v1.0/me"
    _SCOPES = ["Calendars.ReadWrite", "offline_access"]

    def __init__(self, config: Any):
        self._cfg = config
        self._token_cache_file = config.token_cache_file
        self._app: Any = None

    def _get_app(self) -> Any:
        if self._app is not None:
            return self._app
        try:
            import msal  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "msal not installed. Run: pip install 'zen-claw-ai[calendar]'"
            )
        from pathlib import Path

        cache = msal.SerializableTokenCache()
        cache_path = Path(self._token_cache_file).expanduser()
        if cache_path.exists():
            cache.deserialize(cache_path.read_text(encoding="utf-8"))

        self._app = msal.PublicClientApplication(
            client_id=self._cfg.client_id,
            authority=f"https://login.microsoftonline.com/{self._cfg.tenant_id}",
            token_cache=cache,
        )
        return self._app

    def _save_cache(self) -> None:
        from pathlib import Path

        app = self._get_app()
        cache = app.token_cache
        if cache.has_state_changed:
            cache_path = Path(self._token_cache_file).expanduser()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(cache.serialize(), encoding="utf-8")

    def _get_token(self) -> str:
        app = self._get_app()
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(self._SCOPES, account=accounts[0])
        if not result:
            raise RuntimeError(
                "Outlook token expired or missing. Run: zen-claw auth outlook"
            )
        self._save_cache()
        return result["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        import httpx

        r = httpx.get(url, headers=self._headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        import httpx

        r = httpx.post(url, headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _patch(self, url: str, body: dict[str, Any]) -> Any:
        import httpx

        r = httpx.patch(url, headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _delete(self, url: str) -> None:
        import httpx

        r = httpx.delete(url, headers=self._headers(), timeout=30)
        r.raise_for_status()

    def list_events(self, start: str, end: str, _tz: str) -> str:
        url = f"{self._GRAPH_BASE}/calendarView"
        params = {"startDateTime": start, "endDateTime": end, "$top": "50", "$orderby": "start/dateTime"}
        data = self._get(url, params)
        items = data.get("value", [])
        if not items:
            return "No events in the specified range."
        lines = []
        for ev in items:
            title = ev.get("subject", "(no title)")
            s = ev.get("start", {}).get("dateTime", "")
            e = ev.get("end", {}).get("dateTime", "")
            loc = ev.get("location", {}).get("displayName", "")
            lines.append(_fmt_event(title, s, e, loc, ev.get("id", "")))
        return "\n".join(lines)

    def create_event(self, title: str, start: str, end: str, description: str, attendees: list[str], tz: str) -> str:
        body: dict[str, Any] = {
            "subject": title,
            "start": {"dateTime": start, "timeZone": tz or "UTC"},
            "end": {"dateTime": end, "timeZone": tz or "UTC"},
        }
        if description:
            body["body"] = {"contentType": "text", "content": description}
        if attendees:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        ev = self._post(f"{self._GRAPH_BASE}/events", body)
        return f"Created event id={ev['id']}  {title!r}"

    def update_event(self, event_id: str, title: str | None, start: str | None, end: str | None, tz: str) -> str:
        body: dict[str, Any] = {}
        if title:
            body["subject"] = title
        if start:
            body["start"] = {"dateTime": start, "timeZone": tz or "UTC"}
        if end:
            body["end"] = {"dateTime": end, "timeZone": tz or "UTC"}
        ev = self._patch(f"{self._GRAPH_BASE}/events/{event_id}", body)
        return f"Updated event id={ev['id']}"

    def delete_event(self, event_id: str) -> str:
        self._delete(f"{self._GRAPH_BASE}/events/{event_id}")
        return f"Deleted event id={event_id}"

    def find_free_slots(self, start: str, end: str, tz: str) -> str:
        body = {
            "schedules": ["me"],
            "startTime": {"dateTime": start, "timeZone": tz or "UTC"},
            "endTime": {"dateTime": end, "timeZone": tz or "UTC"},
            "availabilityViewInterval": 30,
        }
        data = self._post(f"{self._GRAPH_BASE}/calendar/getSchedule", body)
        schedules = data.get("value", [])
        if not schedules:
            return f"You are free from {start[:16]} to {end[:16]}."
        busy = schedules[0].get("scheduleItems", [])
        if not busy:
            return f"You are free from {start[:16]} to {end[:16]}."
        lines = [f"Busy: {b['start']['dateTime'][:16]} → {b['end']['dateTime'][:16]}  {b.get('subject','')}" for b in busy]
        return "\n".join(lines)


# ── Apple / iCloud CalDAV backend ─────────────────────────────────────────────


class _AppleCalendarBackend:
    def __init__(self, config: Any):
        self._cfg = config
        self._vault: Any = None

    def _get_password(self) -> str:
        if self._vault is None:
            from zen_claw.auth.credentials import CredentialVault

            self._vault = CredentialVault()
        return self._vault.get(self._cfg.credential_platform, self._cfg.credential_key) or ""

    def _get_client(self) -> Any:
        try:
            import caldav  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError("caldav not installed. Run: pip install 'zen-claw-ai[calendar]'")
        password = self._get_password()
        if not password:
            raise RuntimeError(
                "Apple Calendar password not set. Run: zen-claw credentials set apple_calendar password <APP_SPECIFIC_PASSWORD>"
            )
        return caldav.DAVClient(
            url=self._cfg.caldav_url,
            username=self._cfg.username,
            password=password,
        )

    def _get_calendar(self) -> Any:
        client = self._get_client()
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            raise RuntimeError("No calendars found in iCloud account.")
        return calendars[0]

    def list_events(self, start: str, end: str, _tz: str) -> str:
        from datetime import datetime

        cal = self._get_calendar()
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        events = cal.date_search(start=start_dt, end=end_dt, expand=True)
        if not events:
            return "No events in the specified range."
        lines = []
        for ev in events:
            vevent = ev.vobject_instance.vevent
            title = str(getattr(vevent, "summary", "(no title)").value)
            dtstart = str(getattr(vevent, "dtstart", "").value)[:16]
            dtend = str(getattr(vevent, "dtend", "").value)[:16]
            uid = str(getattr(vevent, "uid", "").value)
            lines.append(_fmt_event(title, dtstart, dtend, "", uid))
        return "\n".join(lines)

    def create_event(self, title: str, start: str, end: str, description: str, _attendees: list[str], _tz: str) -> str:
        cal = self._get_calendar()
        uid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "@zen-claw"
        ical = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            f"SUMMARY:{title}\r\n"
            f"DTSTART:{start.replace('-','').replace(':','').replace('+00:00','Z')[:16]}00Z\r\n"
            f"DTEND:{end.replace('-','').replace(':','').replace('+00:00','Z')[:16]}00Z\r\n"
            f"UID:{uid}\r\n"
        )
        if description:
            ical += f"DESCRIPTION:{description}\r\n"
        ical += "END:VEVENT\r\nEND:VCALENDAR\r\n"
        cal.save_event(ical)
        return f"Created event '{title}' in iCloud Calendar."

    def update_event(self, event_id: str, title: str | None, _start: str | None, _end: str | None, _tz: str) -> str:
        # CalDAV update requires fetching the event by UID and patching the iCal data
        cal = self._get_calendar()
        events = cal.events()
        for ev in events:
            vevent = ev.vobject_instance.vevent
            uid = str(getattr(vevent, "uid", "").value)
            if uid == event_id:
                if title:
                    vevent.summary.value = title
                ev.save()
                return f"Updated event uid={event_id}"
        return f"Event uid={event_id} not found."

    def delete_event(self, event_id: str) -> str:
        cal = self._get_calendar()
        events = cal.events()
        for ev in events:
            vevent = ev.vobject_instance.vevent
            uid = str(getattr(vevent, "uid", "").value)
            if uid == event_id:
                ev.delete()
                return f"Deleted event uid={event_id}"
        return f"Event uid={event_id} not found."

    def find_free_slots(self, start: str, end: str, tz: str) -> str:
        # Apple CalDAV doesn't have a free/busy query; fall back to listing busy times
        busy_text = self.list_events(start, end, tz)
        if busy_text.startswith("No events"):
            return f"You are free from {start[:16]} to {end[:16]}."
        return "Busy periods:\n" + busy_text


# ── Main CalendarTool ──────────────────────────────────────────────────────────


class CalendarTool(Tool):
    """Manage calendar events across Google Calendar, Outlook, and Apple Calendar."""

    name = "calendar"
    description = (
        "Manage calendar events. Supports Google Calendar (provider='google'), "
        "Outlook Calendar (provider='outlook'), and Apple/iCloud Calendar (provider='apple'). "
        "Actions: list_events, create_event, update_event, delete_event, find_free_slots."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_events", "create_event", "update_event", "delete_event", "find_free_slots"],
                "description": "Action to perform.",
            },
            "provider": {
                "type": "string",
                "enum": ["google", "outlook", "apple"],
                "description": "Calendar provider to use.",
            },
            "start": {
                "type": "string",
                "description": "Start datetime in ISO 8601 format, e.g. 2026-03-27T09:00:00Z.",
            },
            "end": {
                "type": "string",
                "description": "End datetime in ISO 8601 format.",
            },
            "title": {"type": "string", "description": "Event title."},
            "description": {"type": "string", "description": "Event description/notes."},
            "event_id": {"type": "string", "description": "Event ID (for update/delete)."},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of attendee email addresses.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. Asia/Shanghai. Defaults to UTC.",
            },
        },
        "required": ["action", "provider"],
    }

    def __init__(self, config: "CalendarConfig"):
        self._cfg = config
        self._backends: dict[str, Any] = {}

    def _get_backend(self, provider: str) -> Any:
        if provider in self._backends:
            return self._backends[provider]

        if provider == "google":
            if not self._cfg.google.enabled:
                return "Google Calendar is not enabled. Set tools.calendar.google.enabled = true."
            backend = _GoogleCalendarBackend(self._cfg.google)
        elif provider == "outlook":
            if not self._cfg.outlook.enabled:
                return "Outlook Calendar is not enabled. Set tools.calendar.outlook.enabled = true."
            if not self._cfg.outlook.client_id:
                return "Outlook client_id not configured. Set tools.calendar.outlook.client_id."
            backend = _OutlookCalendarBackend(self._cfg.outlook)
        elif provider == "apple":
            if not self._cfg.apple.enabled:
                return "Apple Calendar is not enabled. Set tools.calendar.apple.enabled = true."
            if not self._cfg.apple.username:
                return "Apple Calendar username not configured. Set tools.calendar.apple.username."
            backend = _AppleCalendarBackend(self._cfg.apple)
        else:
            return f"Unknown provider: {provider!r}. Choose from: google, outlook, apple."

        self._backends[provider] = backend
        return backend

    def _check_config(self) -> str | None:
        if not (
            self._cfg.google.enabled
            or self._cfg.outlook.enabled
            or self._cfg.apple.enabled
        ):
            return (
                "No calendar provider is enabled. "
                "Set tools.calendar.google.enabled / outlook.enabled / apple.enabled = true."
            )
        return None

    async def execute(self, action: str, provider: str, **kwargs: Any) -> ToolResult:
        err = self._check_config()
        if err:
            return ToolResult.failure(ToolErrorKind.PERMISSION, err, code="calendar_not_configured")

        backend = self._get_backend(provider)
        if isinstance(backend, str):
            return ToolResult.failure(
                ToolErrorKind.PARAMETER, backend, code="calendar_provider_unavailable"
            )

        start = _parse_dt(kwargs.get("start"))
        end = _parse_dt(kwargs.get("end"))
        tz = str(kwargs.get("timezone") or "UTC")

        try:
            if action == "list_events":
                result = await asyncio.to_thread(backend.list_events, start, end, tz)
                return ToolResult.success(result)

            elif action == "create_event":
                title = str(kwargs.get("title") or "").strip()
                if not title:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "title is required", code="calendar_missing_title"
                    )
                description = str(kwargs.get("description") or "")
                attendees = list(kwargs.get("attendees") or [])
                result = await asyncio.to_thread(
                    backend.create_event, title, start, end, description, attendees, tz
                )
                return ToolResult.success(result)

            elif action == "update_event":
                event_id = str(kwargs.get("event_id") or "").strip()
                if not event_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "event_id is required", code="calendar_missing_event_id"
                    )
                result = await asyncio.to_thread(
                    backend.update_event,
                    event_id,
                    kwargs.get("title"),
                    kwargs.get("start"),
                    kwargs.get("end"),
                    tz,
                )
                return ToolResult.success(result)

            elif action == "delete_event":
                event_id = str(kwargs.get("event_id") or "").strip()
                if not event_id:
                    return ToolResult.failure(
                        ToolErrorKind.PARAMETER, "event_id is required", code="calendar_missing_event_id"
                    )
                result = await asyncio.to_thread(backend.delete_event, event_id)
                return ToolResult.success(result)

            elif action == "find_free_slots":
                result = await asyncio.to_thread(backend.find_free_slots, start, end, tz)
                return ToolResult.success(result)

            else:
                return ToolResult.failure(
                    ToolErrorKind.PARAMETER,
                    f"Unknown action: {action!r}",
                    code="calendar_unknown_action",
                )

        except RuntimeError as exc:
            return ToolResult.failure(ToolErrorKind.PERMISSION, str(exc), code="calendar_setup_error")
        except Exception as exc:
            return ToolResult.failure(ToolErrorKind.RUNTIME, f"Calendar error: {exc}", code="calendar_error")
