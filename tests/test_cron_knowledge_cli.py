import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.agent.tools.result import ToolResult
from zen_claw.cli.commands import _execute_knowledge_cron_job, app
from zen_claw.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def test_cron_add_persists_knowledge_ingest_payload(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    source_dir = tmp_path / "docs"
    source_dir.mkdir()

    out = CliRunner().invoke(
        app,
        [
            "cron",
            "add",
            "--name",
            "refresh-knowledge",
            "--message",
            "refresh local docs",
            "--every",
            "60",
            "--knowledge-source",
            str(source_dir),
            "--knowledge-notebook",
            "ops_docs",
        ],
    )

    assert out.exit_code == 0
    store = data_dir / "cron" / "jobs.json"
    payload = json.loads(store.read_text(encoding="utf-8"))
    job = payload["jobs"][0]
    assert job["payload"]["knowledgeSource"] == str(source_dir)
    assert job["payload"]["knowledgeNotebook"] == "ops_docs"


async def test_execute_knowledge_cron_job_runs_ingest(tmp_path: Path) -> None:
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")

    job = CronJob(
        id="job-1",
        name="refresh knowledge",
        schedule=CronSchedule(kind="every", every_ms=60000),
        payload=CronPayload(
            message="refresh",
            knowledge_source=str(source_dir),
            knowledge_notebook="cron_docs",
        ),
        state=CronJobState(),
    )

    class _FakeKnowledgeAddTool:
        def __init__(self, data_dir: Path):
            self._data_dir = Path(data_dir)

        async def execute(self, source: str, notebook_id: str = "default", **kwargs):
            knowledge_dir = self._data_dir / "knowledge"
            knowledge_dir.mkdir(parents=True, exist_ok=True)
            (knowledge_dir / "notebooks.json").write_text(
                json.dumps(
                    {
                        "notebooks": [
                            {
                                "id": notebook_id,
                                "name": notebook_id,
                                "created_at": "2026-03-12T00:00:00+08:00",
                                "doc_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return ToolResult.success(
                json.dumps(
                    {
                        "notebook": notebook_id,
                        "source": source,
                        "documents": 1,
                        "chunks_added": 2,
                    },
                    ensure_ascii=False,
                )
            )

    from zen_claw.agent.tools import knowledge as _knowledge_module

    _knowledge_module.KnowledgeAddTool = _FakeKnowledgeAddTool  # type: ignore[assignment]

    result = await _execute_knowledge_cron_job(job, data_dir=tmp_path)

    assert '"notebook": "cron_docs"' in result
    notebooks = (tmp_path / "knowledge" / "notebooks.json").read_text(encoding="utf-8")
    assert '"doc_count": 1' in notebooks
    log_path = tmp_path / "dashboard" / "knowledge_cron.log.jsonl"
    assert log_path.exists()
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["status"] == "ok"
    assert rows[-1]["knowledge_notebook"] == "cron_docs"
