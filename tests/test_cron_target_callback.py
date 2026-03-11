from pathlib import Path

from zen_claw.agent.tools.cron import CronTool
from zen_claw.cron.service import CronService


async def test_cron_tool_adds_target_url_payload(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    tool = CronTool(service)
    tool.set_context("webchat", "u1")
    res = await tool.execute(
        action="add",
        message="ping",
        every_seconds=60,
        target_url="http://127.0.0.1:9999/webhook/trigger/a1",
        target_method="POST",
    )
    assert res.ok is True
    jobs = service.list_jobs(include_disabled=True)
    assert jobs
    assert jobs[0].payload.target_url == "http://127.0.0.1:9999/webhook/trigger/a1"
    assert jobs[0].payload.target_method == "POST"


async def test_cron_tool_adds_knowledge_ingest_payload(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron_jobs.json")
    tool = CronTool(service)
    tool.set_context("webchat", "u1")
    source_dir = tmp_path / "docs"
    source_dir.mkdir()

    res = await tool.execute(
        action="add",
        message="refresh knowledge",
        every_seconds=60,
        knowledge_source=str(source_dir),
        knowledge_notebook="cron_docs",
    )

    assert res.ok is True
    jobs = service.list_jobs(include_disabled=True)
    assert jobs
    assert jobs[0].payload.knowledge_source == str(source_dir)
    assert jobs[0].payload.knowledge_notebook == "cron_docs"
