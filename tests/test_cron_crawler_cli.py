import json
from pathlib import Path

from typer.testing import CliRunner

from zen_claw.cli.commands import _execute_crawler_cron_job, app
from zen_claw.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def test_crawler_run_cli(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    async def _fake_crawl_to_rag(self, source):
        return {
            "crawl_name": source.name,
            "notebook": source.notebook_id,
            "documents": 1,
            "chunks_added": 3,
            "crawl_mode": "http",
        }

    monkeypatch.setattr(
        "zen_claw.skills.crawler.extractor.CrawlerExtractor.crawl_to_rag",
        _fake_crawl_to_rag,
    )

    out = CliRunner().invoke(
        app,
        ["crawler", "run", "https://example.com/news", "--name", "news", "--notebook", "crawler_kb"],
    )

    assert out.exit_code == 0
    assert "Crawled" in out.output
    assert "chunks_added=3" in out.output


def test_crawler_schedule_persists_payload(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    out = CliRunner().invoke(
        app,
        [
            "crawler",
            "schedule",
            "https://example.com/news",
            "--name",
            "news-crawl",
            "--every",
            "60",
            "--notebook",
            "crawler_kb",
            "--browser",
            "--selector",
            ".body",
            "--tenant",
            "tenant-a",
            "--store-backend",
            "memory",
            "--metadata-json",
            '{"topic":"finance"}',
        ],
    )

    assert out.exit_code == 0
    payload = json.loads((data_dir / "cron" / "jobs.json").read_text(encoding="utf-8"))
    job = payload["jobs"][0]
    assert job["payload"]["crawlerSourceUrl"] == "https://example.com/news"
    assert job["payload"]["crawlerNotebook"] == "crawler_kb"
    assert job["payload"]["crawlerUseBrowser"] is True
    assert job["payload"]["crawlerSelector"] == ".body"
    assert job["payload"]["crawlerTenantId"] == "tenant-a"
    assert job["payload"]["crawlerStoreBackend"] == "memory"


def test_crawler_source_add_and_run_source_cli(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    async def _fake_crawl_to_rag(self, source):
        assert source.name == "news"
        assert source.url == "https://example.com/news"
        return {
            "crawl_name": source.name,
            "notebook": source.notebook_id,
            "documents": 0,
            "chunks_added": 0,
            "crawl_mode": "http",
            "change_status": "unchanged",
        }

    monkeypatch.setattr(
        "zen_claw.skills.crawler.extractor.CrawlerExtractor.crawl_to_rag",
        _fake_crawl_to_rag,
    )

    runner = CliRunner()
    add_out = runner.invoke(
        app,
        [
            "crawler",
            "source-add",
            "https://example.com/news",
            "--name",
            "news",
            "--notebook",
            "crawler_kb",
            "--browser",
            "--selector",
            ".body",
        ],
    )
    assert add_out.exit_code == 0
    assert (tmp_path / "crawler" / "sources.json").exists()

    run_out = runner.invoke(app, ["crawler", "run-source", "news"])
    assert run_out.exit_code == 0
    assert "change=unchanged" in run_out.output


def test_crawler_schedule_source_uses_catalog(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    source_catalog = data_dir / "crawler" / "sources.json"
    source_catalog.parent.mkdir(parents=True, exist_ok=True)
    source_catalog.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "news",
                        "url": "https://example.com/news",
                        "notebook": "crawler_kb",
                        "use_browser": True,
                        "selector": ".body",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    out = CliRunner().invoke(
        app,
        [
            "crawler",
            "schedule-source",
            "news",
            "--every",
            "60",
            "--tenant",
            "tenant-a",
            "--store-backend",
            "memory",
        ],
    )

    assert out.exit_code == 0
    payload = json.loads((data_dir / "cron" / "jobs.json").read_text(encoding="utf-8"))
    job = payload["jobs"][0]
    assert job["payload"]["crawlerSourceUrl"] == "https://example.com/news"
    assert job["payload"]["crawlerNotebook"] == "crawler_kb"
    assert job["payload"]["crawlerUseBrowser"] is True


async def test_execute_crawler_cron_job_writes_audit(monkeypatch, tmp_path: Path) -> None:
    job = CronJob(
        id="crawler-1",
        name="news crawl",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            message="crawl",
            crawler_source_name="news",
            crawler_source_url="https://example.com/news",
            crawler_notebook="crawler_kb",
            crawler_use_browser=True,
            crawler_selector=".body",
            crawler_tenant_id="tenant-a",
            crawler_store_backend="memory",
            crawler_metadata_json='{"topic":"finance"}',
        ),
        state=CronJobState(),
    )

    async def _fake_crawl_to_rag(self, source):
        assert source.name == "news"
        assert source.use_browser is True
        return {
            "tenant_id": "tenant-a",
            "notebook": "crawler_kb",
            "documents": 1,
            "chunks_added": 4,
            "store_backend": "memory",
        }

    monkeypatch.setattr(
        "zen_claw.skills.crawler.extractor.CrawlerExtractor.crawl_to_rag",
        _fake_crawl_to_rag,
    )

    result = await _execute_crawler_cron_job(job, data_dir=tmp_path)

    assert '"chunks_added": 4' in result
    rows = [
        json.loads(line)
        for line in (tmp_path / "dashboard" / "crawler_cron.log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert rows[-1]["status"] == "ok"
    assert rows[-1]["source_url"] == "https://example.com/news"
