from pathlib import Path

from zen_claw.agent.tools.cron import CronTool
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.cron.service import CronService


async def test_cron_list_only_shows_current_session_jobs(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")

    owner = CronTool(service)
    owner.set_context("telegram", "user_a")
    await owner.execute(action="add", message="job-a", every_seconds=60)

    other = CronTool(service)
    other.set_context("telegram", "user_b")
    await other.execute(action="add", message="job-b", every_seconds=60)

    listed = await owner.execute(action="list")
    assert listed.ok is True
    assert "job-a" in listed.content
    assert "job-b" not in listed.content


async def test_cron_remove_denies_cross_session_job(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")

    owner = CronTool(service)
    owner.set_context("telegram", "user_a")
    created = await owner.execute(action="add", message="job-a", every_seconds=60)
    assert created.ok is True
    assert "(id: " in created.content
    job_id = created.content.split("(id: ", 1)[1].split(")", 1)[0]

    other = CronTool(service)
    other.set_context("telegram", "user_b")
    denied = await other.execute(action="remove", job_id=job_id)
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.kind == ToolErrorKind.PERMISSION
    assert denied.error.code == "cron_job_not_owned"

    still_exists = await owner.execute(action="list")
    assert still_exists.ok is True
    assert "job-a" in still_exists.content


async def test_cron_denies_disallowed_channel(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    tool = CronTool(service, allowed_channels=["telegram"])
    tool.set_context("discord", "user_a")

    denied = await tool.execute(action="add", message="job-a", every_seconds=60)
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.kind == ToolErrorKind.PERMISSION
    assert denied.error.code == "cron_channel_not_allowed"


async def test_cron_denies_disallowed_action_for_channel(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    tool = CronTool(
        service,
        allowed_actions_by_channel={
            "telegram": ["list"],
        },
    )
    tool.set_context("telegram", "user_a")

    denied = await tool.execute(action="add", message="job-a", every_seconds=60)
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.kind == ToolErrorKind.PERMISSION
    assert denied.error.code == "cron_action_not_allowed"


async def test_cron_remove_requires_confirmation_when_enabled(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    owner = CronTool(service, require_remove_confirmation=True)
    owner.set_context("telegram", "user_a")
    created = await owner.execute(action="add", message="job-a", every_seconds=60)
    assert created.ok is True
    job_id = created.content.split("(id: ", 1)[1].split(")", 1)[0]

    denied = await owner.execute(action="remove", job_id=job_id)
    assert denied.ok is False
    assert denied.error is not None
    assert denied.error.kind == ToolErrorKind.PERMISSION
    assert denied.error.code == "cron_remove_confirmation_required"

    removed = await owner.execute(action="remove", job_id=job_id, confirm=True)
    assert removed.ok is True


