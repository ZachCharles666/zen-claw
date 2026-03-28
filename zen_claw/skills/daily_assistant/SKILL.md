---
name: daily_assistant
description: >
  Daily work assistant routines. Use for: morning briefing (email + calendar summary),
  end-of-day wrap-up, meeting reminders, proactive scheduled notifications, and
  first-time setup of recurring daily workflows.
metadata:
  zen-claw:
    emoji: "🗓"
    scopes: ["email", "calendar", "cron", "message", "notion", "gdrive"]
    requires: []
    trust_level: trusted
---

# Daily Assistant Skill

This skill provides pre-built patterns for running zen-claw as a personal daily work assistant.
It uses the `email`, `calendar`, `cron`, `message`, `notion`, and `gdrive` tools in combination.

---

## Pattern 1: Morning Briefing

When asked for a morning briefing or "today's summary", do the following in order:

1. Call `calendar` with `action="list_events"`, `provider="<configured_provider>"`,
   `start=<today 00:00 UTC>`, `end=<today 23:59 UTC>` to get today's schedule.
2. Call `email` with `action="read_inbox"`, `unread_only=true`, `limit=10`
   to get unread emails.
3. Format a concise briefing:
   - **Today's schedule**: list each event with time and title
   - **Unread emails** (count + top 3 senders/subjects)
   - Any flagged action items you notice
4. Deliver via `message` if running in scheduled/cron mode.

Example schedule command (run at 08:30 on weekdays):
```
cron action="add" name="morning_briefing" message="Run morning briefing: check calendar and unread emails, then send me a summary" cron_expr="30 8 * * 1-5" deliver=true
```

---

## Pattern 2: End-of-Day Wrap-up

When asked for an end-of-day summary or "wrap-up":

1. Call `calendar` to list today's completed events.
2. Call `email` with `action="search"`, `query=<today's date YYYY-MM-DD>` for today's emails.
3. Summarize:
   - What happened today (meetings attended)
   - Outstanding unread emails that need replies
   - Tomorrow's first event (preview)
4. Optionally create a Notion page with the daily summary:
   - `notion action="create_page"` with `parent_id=<your daily-log database>`, `title=<YYYY-MM-DD Wrap-up>`

Example schedule (run at 18:00 on weekdays):
```
cron action="add" name="eod_wrapup" message="End-of-day wrap-up: summarize today's meetings and emails, preview tomorrow" cron_expr="0 18 * * 1-5" deliver=true
```

---

## Pattern 3: Meeting Reminder

For reminders 15 minutes before each meeting:

1. List today's calendar events to find the next one.
2. Schedule a one-shot cron job timed 15 minutes before the event start.
3. The job message: "Remind me: <meeting title> starts in 15 minutes."

Example for a 14:00 meeting:
```
cron action="add" name="meeting_reminder_1400" message="Reminder: your 14:00 meeting starts in 15 minutes" cron_expr="45 13 * * *" deliver=true
```

---

## Pattern 4: First-time Setup

When the user says "set up my daily assistant" or "configure daily routines":

1. Ask which calendar provider they use: google / outlook / apple.
2. Ask their preferred morning briefing time (default 08:30).
3. Ask their preferred end-of-day time (default 18:00).
4. Ask which channel to deliver notifications to (default: current channel).
5. Create the morning briefing and end-of-day cron jobs using `cron action="add"`.
6. Store preferences via `update_user_profile`:
   - `timezone`: user's IANA timezone
   - `preferred_calendar`: google / outlook / apple
   - `briefing_time`: HH:MM
   - `wrapup_time`: HH:MM

---

## Pattern 5: Ad-hoc Reminders

For one-time reminders ("remind me at 3pm to call Alice"):

```
cron action="add" name="reminder_call_alice" message="Reminder: call Alice" cron_expr="0 15 27 3 *" deliver=true
```

Use `at` kind for one-shot: `cron action="add" kind="at" at_ms=<unix_ms> message="..."`.

---

## Notes

- All `deliver=true` cron jobs send the result to the channel where the job was created.
- The `calendar` tool needs the provider explicitly: pass `provider="google"` etc.
- The `email` tool reads from INBOX by default; use `action="search"` for other folders.
- Store recurring context (team members, project names) using `update_user_profile` so
  the morning briefing becomes more relevant over time.
