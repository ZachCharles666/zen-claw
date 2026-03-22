"""Crawler extraction helpers built on top of existing browser and RAG foundations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        if source.use_browser:
            return await self._fetch_text_via_browser(source)
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
        payload["crawl_selector"] = str(extracted.get("selector") or "")
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
        }
