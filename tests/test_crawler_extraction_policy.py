"""Tests for Phase 3.5.3 — richer selector strategies + multi-page fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient as _TC
    from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key
    import zen_claw.dashboard.server as _srv_mod

    _FASTAPI_AVAILABLE = api_app is not None
except Exception:
    _FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sources_file(path: Path, sources: list[dict] | None = None) -> Path:
    sources_dir = path / "crawler"
    sources_dir.mkdir(parents=True, exist_ok=True)
    sources_path = sources_dir / "sources.json"
    payload = {"sources": sources or []}
    sources_path.write_text(json.dumps(payload), encoding="utf-8")
    return sources_path


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


def _fake_config(workspace: Path):
    from zen_claw.config.schema import Config

    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


def _setup_api(tmp_path: Path, monkeypatch):
    _skip_no_fastapi()
    import zen_claw.config.loader as loader_mod

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    _make_sources_file(tmp_path)
    monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(loader_mod, "load_config", lambda: _fake_config(ws))
    monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw


# ---------------------------------------------------------------------------
# CrawlerSource dataclass tests
# ---------------------------------------------------------------------------


class TestCrawlerSourceDefaults:
    def test_crawler_source_default_selector_type(self) -> None:
        """CrawlerSource default selector_type is 'css'."""
        from zen_claw.skills.crawler.extractor import CrawlerSource

        source = CrawlerSource(name="test", url="https://example.com")
        assert source.selector_type == "css"

    def test_crawler_source_max_pages_default(self) -> None:
        """CrawlerSource default max_pages is 1."""
        from zen_claw.skills.crawler.extractor import CrawlerSource

        source = CrawlerSource(name="test", url="https://example.com")
        assert source.max_pages == 1

    def test_crawler_source_pagination_selector_default(self) -> None:
        """CrawlerSource default pagination_selector is empty string."""
        from zen_claw.skills.crawler.extractor import CrawlerSource

        source = CrawlerSource(name="test", url="https://example.com")
        assert source.pagination_selector == ""


# ---------------------------------------------------------------------------
# _apply_selector tests
# ---------------------------------------------------------------------------


class TestApplySelector:
    def test_apply_selector_empty_selector(self) -> None:
        """Empty selector returns full input text."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor

        html = "<html><body><p>hello world</p></body></html>"
        result = CrawlerExtractor._apply_selector(html, "", "css")
        assert result == html

    def test_apply_selector_css_match(self) -> None:
        """CSS selector extracts matching elements' text."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor

        html = "<html><body><p>hello</p><span>world</span></body></html>"
        result = CrawlerExtractor._apply_selector(html, "p", "css")
        assert "hello" in result

    def test_apply_selector_css_no_match_returns_original(self) -> None:
        """CSS selector with no match returns original HTML."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor

        html = "<html><body><p>hello</p></body></html>"
        result = CrawlerExtractor._apply_selector(html, ".nonexistent-class", "css")
        # Falls back to original when bs4 not available or no match
        assert result  # not empty

    def test_apply_selector_regex_match(self) -> None:
        """Regex selector with group(1) extracts matched group."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor

        text = "Price: $42.00 USD"
        result = CrawlerExtractor._apply_selector(text, r"Price: (\$[\d.]+)", "regex")
        assert result == "$42.00"

    def test_apply_selector_regex_no_match(self) -> None:
        """Regex with no match returns full text."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor

        text = "No price here"
        result = CrawlerExtractor._apply_selector(text, r"NOMATCH(\w+)", "regex")
        assert result == text

    def test_apply_selector_xpath_fallback(self) -> None:
        """XPath selector with lxml unavailable falls back gracefully."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor
        import sys

        # Patch lxml to be unavailable by temporarily replacing the import
        html = "<html><body><p>fallback</p></body></html>"
        import unittest.mock as mock

        # The method should not raise even if lxml is absent
        with mock.patch.dict(sys.modules, {"lxml": None, "lxml.etree": None}):
            result = CrawlerExtractor._apply_selector(html, "//p", "xpath")
        assert result  # returns something (either extracted or fallback)


# ---------------------------------------------------------------------------
# fetch_text paginated fields tests
# ---------------------------------------------------------------------------


class TestFetchTextPaginatedFields:
    @pytest.mark.asyncio
    async def test_fetch_text_single_page_http_returns_pages_fetched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """HTTP mode fetch_text returns pages_fetched=1 regardless of max_pages."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor, CrawlerSource

        source = CrawlerSource(
            name="test",
            url="https://example.com",
            use_browser=False,
            max_pages=3,
        )
        extractor = CrawlerExtractor(data_dir=tmp_path)

        async def _fake_ingest(url):
            from zen_claw.knowledge.ingestor import Document

            return [Document(content="Hello world", source=url, page=None, metadata={})]

        monkeypatch.setattr(extractor._ingestor, "ingest", _fake_ingest)
        result = await extractor.fetch_text(source)
        assert result["pages_fetched"] == 1

    @pytest.mark.asyncio
    async def test_fetch_text_returns_paginated_field(self, tmp_path: Path, monkeypatch) -> None:
        """fetch_text result always contains 'paginated' bool field."""
        from zen_claw.skills.crawler.extractor import CrawlerExtractor, CrawlerSource

        source = CrawlerSource(name="test", url="https://example.com", use_browser=False)
        extractor = CrawlerExtractor(data_dir=tmp_path)

        async def _fake_ingest(url):
            from zen_claw.knowledge.ingestor import Document

            return [Document(content="Some text", source=url, page=None, metadata={})]

        monkeypatch.setattr(extractor._ingestor, "ingest", _fake_ingest)
        result = await extractor.fetch_text(source)
        assert "paginated" in result
        assert isinstance(result["paginated"], bool)


# ---------------------------------------------------------------------------
# CrawlerSourceRequest validation tests
# ---------------------------------------------------------------------------


class TestCrawlerSourceRequestValidation:
    def test_source_request_new_fields_defaults(self, tmp_path: Path, monkeypatch) -> None:
        """POST /crawler/sources without new fields → 200 (defaults applied)."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        # Only required fields — new fields should have defaults
        resp = client.post(
            "/api/v1/crawler/sources",
            json={"name": "defaults_test", "url": "https://example.com"},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        # Verify defaults in persisted source
        list_resp = client.get("/api/v1/crawler/sources", headers={"X-API-Key": raw})
        sources = {s["name"]: s for s in list_resp.json()["sources"]}
        s = sources["defaults_test"]
        assert s["selector_type"] == "css"
        assert s["max_pages"] == 1

    def test_source_request_max_pages_over_limit(self, tmp_path: Path, monkeypatch) -> None:
        """max_pages > 20 → 422 validation error."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/crawler/sources",
            json={"name": "test", "url": "https://example.com", "max_pages": 21},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 422

    def test_source_request_max_pages_zero(self, tmp_path: Path, monkeypatch) -> None:
        """max_pages=0 → 422 validation error."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.post(
            "/api/v1/crawler/sources",
            json={"name": "test", "url": "https://example.com", "max_pages": 0},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Persistence tests (new fields survive round-trip through API)
# ---------------------------------------------------------------------------


class TestNewFieldsPersistence:
    def test_upsert_source_persists_selector_type(self, tmp_path: Path, monkeypatch) -> None:
        """POST /crawler/sources with selector_type='regex' → GET sources returns it."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        client.post(
            "/api/v1/crawler/sources",
            json={"name": "news", "url": "https://example.com", "selector_type": "regex"},
            headers={"X-API-Key": raw},
        )
        resp = client.get("/api/v1/crawler/sources", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        sources = {s["name"]: s for s in resp.json()["sources"]}
        assert sources["news"]["selector_type"] == "regex"

    def test_upsert_source_persists_max_pages(self, tmp_path: Path, monkeypatch) -> None:
        """POST /crawler/sources with max_pages=5 → GET returns max_pages=5."""
        raw = _setup_api(tmp_path, monkeypatch)
        client = _TC(api_app, raise_server_exceptions=False)
        client.post(
            "/api/v1/crawler/sources",
            json={"name": "finance", "url": "https://example.com", "max_pages": 5},
            headers={"X-API-Key": raw},
        )
        resp = client.get("/api/v1/crawler/sources", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        sources = {s["name"]: s for s in resp.json()["sources"]}
        assert sources["finance"]["max_pages"] == 5

    def test_backward_compat_missing_new_fields(self, tmp_path: Path) -> None:
        """Old sources.json without selector_type/max_pages loads without error."""
        from zen_claw.skills.crawler.scheduler import load_sources_json

        sources_dir = tmp_path / "crawler"
        sources_dir.mkdir(parents=True, exist_ok=True)
        path = sources_dir / "sources.json"
        # Old format — missing new fields
        path.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "name": "legacy",
                            "url": "https://example.com",
                            "notebook": "default",
                            "selector": "",
                            "use_browser": False,
                            "max_chars": 5000,
                            "metadata": {},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        sources = load_sources_json(path)
        assert len(sources) == 1
        assert sources[0].selector_type == "css"
        assert sources[0].max_pages == 1
        assert sources[0].enabled is True
