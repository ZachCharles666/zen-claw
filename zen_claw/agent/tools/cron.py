"""Cron tool for scheduling reminders and tasks."""

from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.cron.service import CronService
from zen_claw.cron.types import CronJob, CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(
        self,
        cron_service: CronService,
        allowed_channels: list[str] | None = None,
        allowed_actions_by_channel: dict[str, list[str]] | None = None,
        require_remove_confirmation: bool = False,
        max_jobs_per_session: int = 10,
    ):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        self._allowed_channels = {c.strip().lower() for c in (allowed_channels or []) if c.strip()}
        self._require_remove_confirmation = require_remove_confirmation
        self._max_jobs_per_session = max_jobs_per_session
        self._allowed_actions_by_channel: dict[str, set[str]] = {}
        for channel, actions in (allowed_actions_by_channel or {}).items():
            key = (channel or "").strip().lower()
            if not key:
                continue
            normalized = {a.strip().lower() for a in actions if a and a.strip()}
            filtered = {a for a in normalized if a in {"add", "list", "remove"}}
            if filtered:
                self._allowed_actions_by_channel[key] = filtered

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
                "confirm": {
                    "type": "boolean",
                    "description": "Set true to confirm sensitive remove action when required",
                },
                "target_url": {
                    "type": "string",
                    "description": "Optional webhook target URL for external callback trigger",
                },
                "target_method": {
                    "type": "string",
                    "enum": ["POST", "PUT"],
                    "description": "HTTP method used when target_url is set",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        job_id: str | None = None,
        confirm: bool = False,
        target_url: str | None = None,
        target_method: str = "POST",
        **kwargs: Any,
    ) -> ToolResult:
        if not self._is_action_allowed(action):
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"cron action '{action}' is not allowed on channel '{self._channel}'",
                code="cron_action_not_allowed",
            )
        if action == "add":
            return self._add_job(message, every_seconds, cron_expr, target_url, target_method)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            if self._require_remove_confirmation and not confirm:
                return ToolResult.failure(
                    ToolErrorKind.PERMISSION,
                    "remove requires explicit confirmation (confirm=true)",
                    code="cron_remove_confirmation_required",
                )
            return self._remove_job(job_id)
        return ToolResult.failure(
            ToolErrorKind.PARAMETER,
            f"Unknown action: {action}",
            code="cron_unknown_action",
        )

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        target_url: str | None,
        target_method: str,
    ) -> ToolResult:
        if not message:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "message is required for add",
                code="cron_message_required",
            )
        if not self._channel or not self._chat_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "no session context (channel/chat_id)",
                code="cron_context_missing",
            )
        if not self._is_allowed_channel():
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"cron is not allowed on channel '{self._channel}'",
                code="cron_channel_not_allowed",
            )

        # Build schedule
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr)
        else:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "either every_seconds or cron_expr is required",
                code="cron_schedule_required",
            )

        try:
            job = self._cron.add_job(
                name=message[:30],
                schedule=schedule,
                message=message,
                deliver=True,
                channel=self._channel,
                to=self._chat_id,
                target_url=target_url,
                target_method=target_method,
                max_jobs=self._max_jobs_per_session,
            )
            return ToolResult.success(f"Created job '{job.name}' (id: {job.id})")
        except ValueError as e:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                str(e),
                code="cron_limit_reached",
            )

    def _list_jobs(self) -> ToolResult:
        if not self._channel or not self._chat_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "no session context (channel/chat_id)",
                code="cron_context_missing",
            )
        if not self._is_allowed_channel():
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"cron is not allowed on channel '{self._channel}'",
                code="cron_channel_not_allowed",
            )
        jobs = [j for j in self._cron.list_jobs() if self._is_current_session_job(j)]
        if not jobs:
            return ToolResult.success("No scheduled jobs.")
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return ToolResult.success("Scheduled jobs:\n" + "\n".join(lines))

    def _remove_job(self, job_id: str | None) -> ToolResult:
        if not job_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "job_id is required for remove",
                code="cron_job_id_required",
            )
        if not self._channel or not self._chat_id:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                "no session context (channel/chat_id)",
                code="cron_context_missing",
            )
        if not self._is_allowed_channel():
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"cron is not allowed on channel '{self._channel}'",
                code="cron_channel_not_allowed",
            )
        owned = next(
            (
                j
                for j in self._cron.list_jobs(include_disabled=True)
                if j.id == job_id and self._is_current_session_job(j)
            ),
            None,
        )
        if not owned:
            return ToolResult.failure(
                ToolErrorKind.PERMISSION,
                f"Job {job_id} not found in current session",
                code="cron_job_not_owned",
            )
        if self._cron.remove_job(job_id):
            return ToolResult.success(f"Removed job {job_id}")
        return ToolResult.failure(
            ToolErrorKind.PARAMETER,
            f"Job {job_id} not found",
            code="cron_job_not_found",
        )

    def _is_current_session_job(self, job: CronJob) -> bool:
        payload = job.payload
        return payload.channel == self._channel and payload.to == self._chat_id

    def _is_allowed_channel(self) -> bool:
        if not self._allowed_channels:
            return True
        return self._channel.lower() in self._allowed_channels

    def _is_action_allowed(self, action: str) -> bool:
        channel = (self._channel or "").strip().lower()
        if not channel:
            return True
        allowed = self._allowed_actions_by_channel.get(channel)
        if allowed is None:
            return True
        return action in allowed
