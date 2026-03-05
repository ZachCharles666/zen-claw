import json
from pathlib import Path

from zen_claw.cron.service import CronService
from zen_claw.cron.types import CronSchedule
from zen_claw.dashboard.server import trigger_cron_job_with_audit


def test_trigger_cron_job_with_audit_writes_event(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    cron_store = data_dir / "cron" / "jobs.json"
    service = CronService(cron_store)
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="hello",
    )

    result = trigger_cron_job_with_audit(job.id, data_dir=data_dir)
    assert result["ok"] is True
    assert result["job_id"] == job.id
    assert result["trace_id"]

    audit_file = data_dir / "dashboard" / "audit.log.jsonl"
    assert audit_file.exists()
    lines = [line for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    event = json.loads(lines[-1])
    assert event["event"] == "dashboard.cron.run"
    assert event["job_id"] == job.id
    assert event["ok"] is True


def test_trigger_cron_job_with_audit_not_found(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = trigger_cron_job_with_audit("missing-id", data_dir=data_dir)
    assert result["ok"] is False
