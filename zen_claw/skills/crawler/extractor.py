"""Crawler extraction helpers built on top of existing browser and RAG foundations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from zen_claw.agent.tools.browser import BrowserExtractTool, BrowserOpenTool
from zen_claw.knowledge.ingestor import Document, Ingestor
from zen_claw.knowledge.pipeline import RAGPipeline


@dataclass
class CrawlerSource:
    """One crawler source definition."""

    name: str
    url: str
    notebook_id: str = "default"
    selector: str = ""
    use_browser: bool = False
    max_chars: int = 20_000
    metadata: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    selector_type: str = "css"          # "css" | "xpath" | "regex"
    pagination_selector: str = ""       # CSS selector for next-page link/button
    max_pages: int = 1                  # max pages to follow (1 = no pagination)


class CrawlerExtractor:
    """Extract and ingest crawler content into the existing RAG pipeline."""

    def __init__(
        self,
        data_dir: Path,
        *,
        tenant_id: str = "default",
        store_kind: str = "",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._tenant_id = str(tenant_id or "default").strip() or "default"
        self._store_kind = str(store_kind or "").strip()
        self._ingestor = Ingestor()
        self._max_fetch_attempts = 2

    @staticmethod
    def _apply_selector(html: str, selector: str, selector_type: str = "css") -> str:
        """Extract text from HTML using selector strategy.

        selector_type:
          - "css"   : BeautifulSoup CSS select (default)
          - "xpath" : lxml XPath (graceful fallback if lxml unavailable)
          - "regex" : re.search on html, returns group(1) if match else full text
        Returns full text when selector is empty.
        """
        import re as _re

        if not selector:
            return html
        stype = str(selector_type or "css").strip().lower()
        if stype == "regex":
            try:
                m = _re.search(selector, html, _re.DOTALL)
                return m.group(1) if m and m.lastindex else (m.group(0) if m else html)
            except Exception:
                return html
        if stype == "xpath":
            try:
                from lxml import etree as _et  # type: ignore

                tree = _et.fromstring(html.encode("utf-8", errors="ignore"), parser=_et.HTMLParser())
                nodes = tree.xpath(selector)
                texts = []
                for node in nodes:
                    if hasattr(node, "text_content"):
                        texts.append(node.text_content())
                    else:
                        texts.append(str(node))
                return "\n".join(texts) if texts else html
            except Exception:
                return html
        # Default: CSS via BeautifulSoup
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            nodes = soup.select(selector)
            return "\n".join(n.get_text(separator=" ", strip=True) for n in nodes) if nodes else html
        except Exception:
            return html

    async def fetch_text(self, source: CrawlerSource) -> dict[str, Any]:
        """Fetch normalized text from one crawler source."""
        clean_url = str(source.url or "").strip()
        if not clean_url:
            raise ValueError("crawler source url is required")
        if source.selector and not source.use_browser:
            raise ValueError("selector requires browser extraction mode")
        fetch_strategy = (
            "browser"
            if source.use_browser
            else "http_paginated"
            if source.pagination_selector or int(source.max_pages or 1) > 1
            else "http"
        )
        attempt_errors: list[dict[str, Any]] = []
        for attempt in range(1, self._max_fetch_attempts + 1):
            try:
                payload = await self._fetch_text_once(source, fetch_strategy=fetch_strategy)
                payload["fetch_strategy"] = fetch_strategy
                payload["attempt_count"] = attempt
                payload["retry_count"] = max(0, attempt - 1)
                payload["selector_type"] = str(source.selector_type or "css").strip().lower()
                payload["pagination_selector"] = str(source.pagination_selector or "").strip()
                payload["max_pages"] = max(1, int(source.max_pages or 1))
                if attempt_errors:
                    payload["attempt_errors"] = list(attempt_errors)
                return payload
            except Exception as exc:
                attempt_errors.append(
                    {
                        "attempt": attempt,
                        "error": str(exc),
                        "fetch_strategy": fetch_strategy,
                    }
                )
                if attempt >= self._max_fetch_attempts:
                    raise RuntimeError(
                        f"crawler fetch failed after {attempt} attempts: {exc}"
                    ) from exc
        raise RuntimeError("crawler fetch failed without a terminal error")

    async def _fetch_text_once(
        self,
        source: CrawlerSource,
        *,
        fetch_strategy: str,
    ) -> dict[str, Any]:
        clean_url = str(source.url or "").strip()
        if fetch_strategy == "browser":
            return await self._fetch_text_via_browser(source)
        if fetch_strategy == "http_paginated":
            return await self._fetch_text_via_http_pages(source)
        docs = await self._ingestor.ingest(clean_url)
        text = "\n\n".join(doc.content.strip() for doc in docs if str(doc.content).strip()).strip()
        if not text:
            raise ValueError(f"crawler source returned no extractable text: {clean_url}")
        return {
            "name": source.name,
            "url": clean_url,
            "content": text[: max(100, int(source.max_chars or 20_000))],
            "mode": "http",
            "selector": "",
            "pages_fetched": 1,
            "paginated": False,
            "visited_urls": [clean_url],
        }

    async def crawl_to_rag(self, source: CrawlerSource) -> dict[str, Any]:
        """Extract crawler content and ingest it as a first-class RAG document."""
        extracted = await self.fetch_text(source)
        content_hash = hashlib.sha256(
            str(extracted["content"]).encode("utf-8", errors="ignore")
        ).hexdigest()
        metadata = {
            **dict(source.metadata or {}),
            "source_type": extracted["mode"],
            "crawl_name": str(source.name or "").strip(),
            "crawl_url": extracted["url"],
            "crawl_content_sha256": content_hash,
            "crawl_fetched_at": datetime.now(UTC).isoformat(),
            "crawl_pages_fetched": int(extracted.get("pages_fetched") or 1),
            "crawl_paginated": bool(extracted.get("paginated")),
            "crawl_fetch_strategy": str(extracted.get("fetch_strategy") or extracted["mode"]),
            "crawl_attempt_count": int(extracted.get("attempt_count") or 1),
            "crawl_retry_count": int(extracted.get("retry_count") or 0),
            "crawl_selector_type": str(extracted.get("selector_type") or source.selector_type or "css"),
        }
        if extracted.get("selector"):
            metadata["crawl_selector"] = extracted["selector"]
        docs = [
            Document(
                content=str(extracted["content"]),
                source=str(extracted["url"]),
                page=None,
                metadata=metadata,
            )
        ]
        pipeline = RAGPipeline(
            self._data_dir,
            store_kind=self._store_kind,
            tenant_id=self._tenant_id,
        )
        existing = self._find_existing_document(
            pipeline.list_documents(notebook_id=source.notebook_id),
            source_url=str(extracted["url"]),
        )
        if existing is not None:
            existing_hash = str(existing.get("metadata", {}).get("crawl_content_sha256", "")).strip()
            if existing_hash and existing_hash == content_hash:
                return {
                    "tenant_id": self._tenant_id,
                    "notebook": source.notebook_id,
                    "notebook_id": source.notebook_id,
                    "document_id": str(existing.get("document_id", "")),
                    "source": str(extracted["url"]),
                    "documents": 0,
                    "chunks_added": 0,
                    "metadata": metadata,
                    "store_backend": pipeline.store_kind,
                    "crawl_name": str(source.name or "").strip(),
                    "crawl_mode": extracted["mode"],
                    "fetch_strategy": str(extracted.get("fetch_strategy") or extracted["mode"]),
                    "attempt_count": int(extracted.get("attempt_count") or 1),
                    "retry_count": int(extracted.get("retry_count") or 0),
                    "pages_fetched": int(extracted.get("pages_fetched") or 1),
                    "paginated": bool(extracted.get("paginated")),
                    "crawl_selector": str(extracted.get("selector") or ""),
                    "change_status": "unchanged",
                    "skipped": True,
                }
            pipeline.delete_document(
                str(existing.get("document_id", "")),
                notebook_id=source.notebook_id,
            )
        payload = await pipeline.ingest_documents(
            docs,
            notebook_id=source.notebook_id,
            source=str(extracted["url"]),
        )
        payload["crawl_name"] = str(source.name or "").strip()
        payload["crawl_mode"] = extracted["mode"]
        payload["fetch_strategy"] = str(extracted.get("fetch_strategy") or extracted["mode"])
        payload["attempt_count"] = int(extracted.get("attempt_count") or 1)
        payload["retry_count"] = int(extracted.get("retry_count") or 0)
        payload["crawl_selector"] = str(extracted.get("selector") or "")
        payload["pages_fetched"] = int(extracted.get("pages_fetched") or 1)
        payload["paginated"] = bool(extracted.get("paginated"))
        payload["visited_urls"] = list(extracted.get("visited_urls") or [str(extracted["url"])])
        payload["change_status"] = "updated" if existing is not None else "new"
        payload["skipped"] = False
        return payload

    @staticmethod
    def _find_existing_document(
        listing_payload: dict[str, Any],
        *,
        source_url: str,
    ) -> dict[str, Any] | None:
        clean_source = str(source_url or "").strip()
        if not clean_source:
            return None
        rows = listing_payload.get("documents", [])
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("source", "")).strip() == clean_source:
                return row
        return None

    async def _fetch_text_via_browser(self, source: CrawlerSource) -> dict[str, Any]:
        from zen_claw.config.loader import load_config

        browser_cfg = load_config().tools.effective_browser()
        open_tool = BrowserOpenTool(
            mode=browser_cfg.mode,
            sidecar_url=browser_cfg.sidecar_url,
            sidecar_approval_token=browser_cfg.sidecar_approval_token.get_secret_value(),
            sidecar_healthcheck=browser_cfg.sidecar_healthcheck,
            sidecar_fallback_to_off=browser_cfg.sidecar_fallback_to_off,
            allowed_domains=browser_cfg.allowed_domains,
            blocked_domains=browser_cfg.blocked_domains,
            max_steps=browser_cfg.max_steps,
            timeout_sec=browser_cfg.timeout_sec,
        )
        extract_tool = BrowserExtractTool(
            mode=browser_cfg.mode,
            sidecar_url=browser_cfg.sidecar_url,
            sidecar_approval_token=browser_cfg.sidecar_approval_token.get_secret_value(),
            sidecar_healthcheck=browser_cfg.sidecar_healthcheck,
            sidecar_fallback_to_off=browser_cfg.sidecar_fallback_to_off,
            allowed_domains=browser_cfg.allowed_domains,
            blocked_domains=browser_cfg.blocked_domains,
            max_steps=browser_cfg.max_steps,
            timeout_sec=browser_cfg.timeout_sec,
        )
        opened = await open_tool.execute(url=source.url, maxSteps=min(browser_cfg.max_steps, 6))
        if not opened.ok:
            message = opened.error.message if opened.error else "browser open failed"
            raise RuntimeError(message)
        open_payload = json.loads(opened.content)
        session_id = str(open_payload.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("browser open did not return session_id")
        extracted = await extract_tool.execute(
            sessionId=session_id,
            selector=source.selector or None,
            maxChars=max(100, int(source.max_chars or 20_000)),
            maxSteps=min(browser_cfg.max_steps, 6),
        )
        if not extracted.ok:
            message = extracted.error.message if extracted.error else "browser extract failed"
            raise RuntimeError(message)
        extract_payload = json.loads(extracted.content)
        text = str(extract_payload.get("text") or "").strip()
        if not text:
            raise ValueError(f"crawler source returned no extractable text: {source.url}")
        return {
            "name": source.name,
            "url": str(open_payload.get("final_url") or source.url),
            "content": text,
            "mode": "browser",
            "selector": str(source.selector or "").strip(),
            "pages_fetched": 1,
            "paginated": False,
            "visited_urls": [str(open_payload.get("final_url") or source.url)],
        }

    async def _fetch_text_via_http_pages(self, source: CrawlerSource) -> dict[str, Any]:
        current_url = str(source.url or "").strip()
        if not current_url:
            raise ValueError("crawler source url is required")
        collected: list[str] = []
        visited: set[str] = set()
        visited_urls: list[str] = []
        pages_fetched = 0
        max_pages = max(1, int(source.max_pages or 1))
        while current_url and pages_fetched < max_pages:
            normalized_url = str(current_url).strip()
            if not normalized_url or normalized_url in visited:
                break
            visited.add(normalized_url)
            visited_urls.append(normalized_url)
            docs = await self._ingestor.ingest(normalized_url)
            text = "\n\n".join(doc.content.strip() for doc in docs if str(doc.content).strip()).strip()
            if text:
                collected.append(text)
            pages_fetched += 1
            if not source.pagination_selector or pages_fetched >= max_pages:
                break
            html = self._fetch_http_html(normalized_url)
            current_url = self._extract_next_page_url(
                html,
                current_url=normalized_url,
                pagination_selector=source.pagination_selector,
            )
        merged = "\n\n".join(part for part in collected if part).strip()
        if not merged:
            raise ValueError(f"crawler source returned no extractable text: {source.url}")
        return {
            "name": source.name,
            "url": str(source.url),
            "content": merged[: max(100, int(source.max_chars or 20_000))],
            "mode": "http",
            "selector": "",
            "pages_fetched": pages_fetched or 1,
            "paginated": pages_fetched > 1,
            "visited_urls": visited_urls,
        }

    @staticmethod
    def _fetch_http_html(url: str) -> str:
        req = Request(
            str(url),
            headers={
                "User-Agent": "zen-claw-crawler/alpha",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="ignore")

    @staticmethod
    def _extract_next_page_url(
        html: str,
        *,
        current_url: str,
        pagination_selector: str,
    ) -> str:
        import re

        selector = str(pagination_selector or "").strip()
        if not selector:
            return ""
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            node = soup.select_one(selector)
            if node is None:
                return ""
            href = str(node.get("href") or "").strip()
            return urljoin(current_url, href) if href else ""
        except Exception:
            class_name = selector[1:] if selector.startswith(".") else ""
            if class_name:
                pattern = re.compile(
                    rf'<a[^>]*class="[^"]*\b{re.escape(class_name)}\b[^"]*"[^>]*href="([^"]+)"',
                    re.IGNORECASE,
                )
                match = pattern.search(html)
                if match:
                    return urljoin(current_url, match.group(1))
            return ""
