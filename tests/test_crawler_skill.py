import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zen_claw.knowledge.ingestor import Document
from zen_claw.skills.crawler.extractor import CrawlerExtractor, CrawlerSource
from zen_claw.skills.crawler.scheduler import (
    build_crawler_cron_kwargs,
    get_source_by_name,
    load_sources_json,
    resolve_sources_path,
    upsert_source,
)


@pytest.mark.asyncio
async def test_crawler_fetch_text_http(monkeypatch, tmp_path: Path) -> None:
    async def _fake_ingest(self, source: str):
        assert source == "https://example.com/news"
        return [Document(content="Alpha\nBeta", source=source, metadata={"lang": "en"})]

    monkeypatch.setattr("zen_claw.skills.crawler.extractor.Ingestor.ingest", _fake_ingest)

    extractor = CrawlerExtractor(tmp_path)
    payload = await extractor.fetch_text(
        CrawlerSource(name="news", url="https://example.com/news", max_chars=2000)
    )

    assert payload["mode"] == "http"
    assert payload["url"] == "https://example.com/news"
    assert "Alpha" in payload["content"]


@pytest.mark.asyncio
async def test_crawler_fetch_text_browser(monkeypatch, tmp_path: Path) -> None:
    class _Secret:
        def get_secret_value(self) -> str:
            return ""

    class _BrowserCfg:
        mode = "sidecar"
        sidecar_url = "http://127.0.0.1:4500/v1/browser"
        sidecar_approval_token = _Secret()
        sidecar_healthcheck = False
        sidecar_fallback_to_off = False
        allowed_domains = []
        blocked_domains = []
        max_steps = 20
        timeout_sec = 30

    class _Tools:
        @staticmethod
        def effective_browser():
            return _BrowserCfg()

    class _Cfg:
        tools = _Tools()

    async def _fake_open(self, **kwargs):
        return SimpleNamespace(
            ok=True,
            error=None,
            content=json.dumps({"session_id": "s1", "final_url": "https://example.com/final"}),
        )

    async def _fake_extract(self, **kwargs):
        assert kwargs["sessionId"] == "s1"
        assert kwargs["selector"] == ".article"
        return SimpleNamespace(
            ok=True,
            error=None,
            content=json.dumps({"text": "Rendered body"}),
        )

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: _Cfg())
    monkeypatch.setattr("zen_claw.agent.tools.browser.BrowserOpenTool.execute", _fake_open)
    monkeypatch.setattr("zen_claw.agent.tools.browser.BrowserExtractTool.execute", _fake_extract)

    extractor = CrawlerExtractor(tmp_path)
    payload = await extractor.fetch_text(
        CrawlerSource(
            name="news",
            url="https://example.com/news",
            selector=".article",
            use_browser=True,
        )
    )

    assert payload["mode"] == "browser"
    assert payload["url"] == "https://example.com/final"
    assert payload["content"] == "Rendered body"


@pytest.mark.asyncio
async def test_crawler_crawl_to_rag_uses_pipeline_ingest_documents(
    monkeypatch, tmp_path: Path
) -> None:
    async def _fake_fetch_text(self, source: CrawlerSource):
        return {
            "name": source.name,
            "url": source.url,
            "content": "Crawler content",
            "mode": "browser",
            "selector": ".body",
        }

    class _FakePipeline:
        def __init__(self, data_dir: Path, store_kind: str = "", tenant_id: str = "default", default_notebook: str = "default"):
            assert Path(data_dir) == tmp_path
            assert tenant_id == "tenant-a"
            assert store_kind == "memory"

        @property
        def store_kind(self) -> str:
            return "memory"

        def list_documents(self, *, notebook_id: str = ""):
            assert notebook_id == "crawler_kb"
            return {"documents": []}

        async def ingest_documents(self, docs, *, notebook_id: str = "", source: str = "", metadata=None):
            assert notebook_id == "crawler_kb"
            assert source == "https://example.com/news"
            assert len(docs) == 1
            assert docs[0].source == "https://example.com/news"
            assert docs[0].metadata["crawl_name"] == "news"
            assert docs[0].metadata["crawl_selector"] == ".body"
            return {
                "tenant_id": "tenant-a",
                "notebook": notebook_id,
                "documents": 1,
                "chunks_added": 2,
                "store_backend": "memory",
            }

    monkeypatch.setattr("zen_claw.skills.crawler.extractor.CrawlerExtractor.fetch_text", _fake_fetch_text)
    monkeypatch.setattr("zen_claw.skills.crawler.extractor.RAGPipeline", _FakePipeline)

    extractor = CrawlerExtractor(tmp_path, tenant_id="tenant-a", store_kind="memory")
    payload = await extractor.crawl_to_rag(
        CrawlerSource(
            name="news",
            url="https://example.com/news",
            notebook_id="crawler_kb",
            selector=".body",
            use_browser=True,
            metadata={"topic": "finance"},
        )
    )

    assert payload["crawl_mode"] == "browser"
    assert payload["chunks_added"] == 2
    assert payload["change_status"] == "new"


@pytest.mark.asyncio
async def test_crawler_crawl_to_rag_skips_unchanged_content(monkeypatch, tmp_path: Path) -> None:
    async def _fake_fetch_text(self, source: CrawlerSource):
        return {
            "name": source.name,
            "url": source.url,
            "content": "Crawler content",
            "mode": "http",
            "selector": "",
        }

    class _FakePipeline:
        def __init__(self, data_dir: Path, store_kind: str = "", tenant_id: str = "default", default_notebook: str = "default"):
            self.deleted = []

        @property
        def store_kind(self) -> str:
            return "memory"

        def list_documents(self, *, notebook_id: str = ""):
            return {
                "documents": [
                        {
                            "document_id": "doc-1",
                            "source": "https://example.com/news",
                            "metadata": {
                                "crawl_content_sha256": "bc293f77a27359255cd42e69c03803910eaa2f09b70d642aa747a517ad6670f4"
                            },
                        }
                    ]
                }

        def delete_document(self, document_id: str, *, notebook_id: str = ""):
            raise AssertionError("unchanged crawl should not delete")

        async def ingest_documents(self, docs, *, notebook_id: str = "", source: str = "", metadata=None):
            raise AssertionError("unchanged crawl should not ingest")

    monkeypatch.setattr("zen_claw.skills.crawler.extractor.CrawlerExtractor.fetch_text", _fake_fetch_text)
    monkeypatch.setattr("zen_claw.skills.crawler.extractor.RAGPipeline", _FakePipeline)

    extractor = CrawlerExtractor(tmp_path, tenant_id="tenant-a", store_kind="memory")
    payload = await extractor.crawl_to_rag(
        CrawlerSource(name="news", url="https://example.com/news", notebook_id="crawler_kb")
    )

    assert payload["change_status"] == "unchanged"
    assert payload["skipped"] is True
    assert payload["chunks_added"] == 0


@pytest.mark.asyncio
async def test_crawler_crawl_to_rag_replaces_changed_content(monkeypatch, tmp_path: Path) -> None:
    async def _fake_fetch_text(self, source: CrawlerSource):
        return {
            "name": source.name,
            "url": source.url,
            "content": "Crawler content v2",
            "mode": "http",
            "selector": "",
        }

    class _FakePipeline:
        deleted: list[str] = []

        def __init__(self, data_dir: Path, store_kind: str = "", tenant_id: str = "default", default_notebook: str = "default"):
            pass

        @property
        def store_kind(self) -> str:
            return "memory"

        def list_documents(self, *, notebook_id: str = ""):
            return {
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source": "https://example.com/news",
                        "metadata": {"crawl_content_sha256": "old-hash"},
                    }
                ]
            }

        def delete_document(self, document_id: str, *, notebook_id: str = ""):
            self.deleted.append(document_id)
            return {"ok": True}

        async def ingest_documents(self, docs, *, notebook_id: str = "", source: str = "", metadata=None):
            assert self.deleted == ["doc-1"]
            return {
                "tenant_id": "tenant-a",
                "notebook": notebook_id,
                "documents": 1,
                "chunks_added": 5,
                "store_backend": "memory",
            }

    monkeypatch.setattr("zen_claw.skills.crawler.extractor.CrawlerExtractor.fetch_text", _fake_fetch_text)
    monkeypatch.setattr("zen_claw.skills.crawler.extractor.RAGPipeline", _FakePipeline)

    extractor = CrawlerExtractor(tmp_path, tenant_id="tenant-a", store_kind="memory")
    payload = await extractor.crawl_to_rag(
        CrawlerSource(name="news", url="https://example.com/news", notebook_id="crawler_kb")
    )

    assert payload["change_status"] == "updated"
    assert payload["chunks_added"] == 5


def test_crawler_scheduler_loads_json_and_builds_kwargs(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "news",
                        "url": "https://example.com/news",
                        "notebook": "crawler_kb",
                        "selector": ".body",
                        "use_browser": True,
                        "max_chars": 5000,
                        "metadata": {"topic": "finance"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = load_sources_json(config_path)
    assert len(rows) == 1
    assert rows[0].name == "news"
    kwargs = build_crawler_cron_kwargs(rows[0], tenant_id="tenant-a", store_backend="memory")
    assert kwargs["crawler_source_url"] == "https://example.com/news"
    assert kwargs["crawler_notebook"] == "crawler_kb"
    assert kwargs["crawler_use_browser"] is True
    assert json.loads(kwargs["crawler_metadata_json"])["topic"] == "finance"


def test_crawler_scheduler_upserts_and_resolves_catalog(tmp_path: Path) -> None:
    catalog_path = resolve_sources_path(tmp_path)
    assert catalog_path == tmp_path / "crawler" / "sources.json"

    upsert_source(
        catalog_path,
        CrawlerSource(
            name="news",
            url="https://example.com/news",
            notebook_id="crawler_kb",
            use_browser=True,
            selector=".body",
        ),
    )
    upsert_source(
        catalog_path,
        CrawlerSource(
            name="news",
            url="https://example.com/news-v2",
            notebook_id="crawler_kb_v2",
        ),
    )

    rows = load_sources_json(catalog_path)
    assert len(rows) == 1
    assert rows[0].url == "https://example.com/news-v2"
    assert get_source_by_name(catalog_path, "news").notebook_id == "crawler_kb_v2"
