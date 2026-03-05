from pathlib import Path

import pytest

from zen_claw.agent.tools.cron import CronTool
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.cron.service import CronService


@pytest.mark.asyncio
async def test_cron_limit_enforced_per_session(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    # Limit to 2 jobs per session
    tool = CronTool(service, max_jobs_per_session=2)
    tool.set_context("telegram", "user_a")

    # Add 2 jobs
    res1 = await tool.execute(action="add", message="job-1", every_seconds=60)
    assert res1.ok is True
    res2 = await tool.execute(action="add", message="job-2", every_seconds=60)
    assert res2.ok is True

    # Add 3rd job - should fail
    res3 = await tool.execute(action="add", message="job-3", every_seconds=60)
    assert res3.ok is False
    assert res3.error is not None
    assert res3.error.kind == ToolErrorKind.PERMISSION
    assert res3.error.code == "cron_limit_reached"
    assert "Max cron jobs (2) reached" in res3.error.message


@pytest.mark.asyncio
async def test_cron_limit_is_independent_per_session(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    tool = CronTool(service, max_jobs_per_session=1)

    # User A adds 1 job
    tool.set_context("telegram", "user_a")
    res_a1 = await tool.execute(action="add", message="job-a1", every_seconds=60)
    assert res_a1.ok is True

    # User B adds 1 job - should succeed despite User A's job
    tool.set_context("telegram", "user_b")
    res_b1 = await tool.execute(action="add", message="job-b1", every_seconds=60)
    assert res_b1.ok is True

    # User A adds another - should fail
    tool.set_context("telegram", "user_a")
    res_a2 = await tool.execute(action="add", message="job-a2", every_seconds=60)
    assert res_a2.ok is False
