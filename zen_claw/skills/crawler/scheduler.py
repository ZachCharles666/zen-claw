"""Crawler scheduling helpers on top of the existing cron service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.skills.crawler.extractor import CrawlerSource


def build_crawler_cron_kwargs(
    source: CrawlerSource,
    *,
    tenant_id: str = "default",
    store_backend: str = "",
) -> dict[str, Any]:
    """Translate one crawler source into CronService.add_job kwargs."""
    return {
        "crawler_source_name": str(source.name or "").strip(),
        "crawler_source_url": str(source.url or "").strip(),
        "crawler_notebook": str(source.notebook_id or "default").strip() or "default",
        "crawler_selector": str(source.selector or "").strip() or None,
        "crawler_use_browser": bool(source.use_browser),
        "crawler_max_chars": max(100, int(source.max_chars or 20_000)),
        "crawler_metadata_json": json.dumps(dict(source.metadata or {}), ensure_ascii=False),
        "crawler_tenant_id": str(tenant_id or "default").strip() or "default",
        "crawler_store_backend": str(store_backend or "").strip() or None,
    }


def load_sources_json(path: Path) -> list[CrawlerSource]:
    """Load crawler sources from a JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("sources", [])
    if not isinstance(rows, list):
        raise ValueError("crawler sources file must be a JSON array or {\"sources\": [...]}")
    sources: list[CrawlerSource] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        url = str(row.get("url") or "").strip()
        if not name or not url:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        sources.append(
            CrawlerSource(
                name=name,
                url=url,
                notebook_id=str(row.get("notebook_id") or row.get("notebook") or "default").strip()
                or "default",
                selector=str(row.get("selector") or "").strip(),
                use_browser=bool(row.get("use_browser", False)),
                max_chars=max(100, int(row.get("max_chars") or 20_000)),
                metadata=dict(metadata),
                enabled=bool(row.get("enabled", True)),
                selector_type=str(row.get("selector_type") or "css").strip() or "css",
                pagination_selector=str(row.get("pagination_selector") or "").strip(),
                max_pages=max(1, int(row.get("max_pages") or 1)),
            )
        )
    return sources


def save_sources_json(path: Path, sources: list[CrawlerSource]) -> Path:
    """Persist crawler sources as JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sources": [
            {
                "name": source.name,
                "url": source.url,
                "notebook": source.notebook_id,
                "selector": source.selector,
                "use_browser": bool(source.use_browser),
                "max_chars": max(100, int(source.max_chars or 20_000)),
                "metadata": dict(source.metadata or {}),
                "enabled": bool(source.enabled),
                "selector_type": str(source.selector_type or "css"),
                "pagination_selector": str(source.pagination_selector or ""),
                "max_pages": max(1, int(source.max_pages or 1)),
            }
            for source in sources
        ]
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def resolve_sources_path(data_dir: Path, path: Path | str | None = None) -> Path:
    """Resolve the crawler sources catalog path."""
    if path:
        return Path(path)
    return Path(data_dir) / "crawler" / "sources.json"


def upsert_source(path: Path, source: CrawlerSource) -> Path:
    """Insert or replace one crawler source by name."""
    rows = load_sources_json(path) if Path(path).exists() else []
    clean_name = str(source.name or "").strip().lower()
    out: list[CrawlerSource] = []
    replaced = False
    for row in rows:
        if row.name.strip().lower() == clean_name:
            out.append(source)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(source)
    out.sort(key=lambda item: item.name.lower())
    return save_sources_json(path, out)


def get_source_by_name(path: Path, name: str) -> CrawlerSource:
    """Load one crawler source from a catalog file by name."""
    clean_name = str(name or "").strip().lower()
    if not clean_name:
        raise ValueError("crawler source name cannot be empty")
    for source in load_sources_json(path):
        if source.name.strip().lower() == clean_name:
            return source
    raise ValueError(f"crawler source not found: {name}")


def delete_source(path: Path, name: str) -> bool:
    """Remove source by name. Returns True if found and removed, False if not found."""
    rows = load_sources_json(path) if Path(path).exists() else []
    clean_name = str(name or "").strip().lower()
    original_len = len(rows)
    rows = [s for s in rows if s.name.strip().lower() != clean_name]
    if len(rows) == original_len:
        return False
    save_sources_json(path, rows)
    return True


def set_source_enabled(path: Path, name: str, enabled: bool) -> bool:
    """Set enabled flag on a source by name. Returns True if found, False if not found."""
    from dataclasses import replace as dc_replace

    rows = load_sources_json(path) if Path(path).exists() else []
    clean_name = str(name or "").strip().lower()
    found = False
    updated: list[CrawlerSource] = []
    for s in rows:
        if s.name.strip().lower() == clean_name:
            updated.append(dc_replace(s, enabled=bool(enabled)))
            found = True
        else:
            updated.append(s)
    if found:
        save_sources_json(path, updated)
    return found


# ---------------------------------------------------------------------------
# Schedule management helpers
# ---------------------------------------------------------------------------


def resolve_schedules_path(data_dir: Path) -> Path:
    """Returns {data_dir}/crawler/schedules.json."""
    return Path(data_dir) / "crawler" / "schedules.json"


def load_schedules_json(path: Path) -> list[dict]:
    """Load crawler schedules from JSON. Returns [] if file absent."""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("schedules", [])
    return [r for r in rows if isinstance(r, dict)]


def save_schedules_json(path: Path, schedules: list[dict]) -> Path:
    """Persist crawler schedules as JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schedules": list(schedules)}
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def upsert_schedule(path: Path, schedule: dict) -> None:
    """Insert or replace schedule by source_name."""
    rows = load_schedules_json(path)
    source_name = str(schedule.get("source_name") or "").strip().lower()
    updated: list[dict] = []
    replaced = False
    for row in rows:
        if str(row.get("source_name") or "").strip().lower() == source_name:
            updated.append(dict(schedule))
            replaced = True
        else:
            updated.append(row)
    if not replaced:
        updated.append(dict(schedule))
    save_schedules_json(path, updated)


def delete_schedule(path: Path, source_name: str) -> bool:
    """Remove schedule by source_name. Returns True if found and removed."""
    rows = load_schedules_json(path)
    clean = str(source_name or "").strip().lower()
    filtered = [r for r in rows if str(r.get("source_name") or "").strip().lower() != clean]
    if len(filtered) == len(rows):
        return False
    save_schedules_json(path, filtered)
    return True


def set_schedule_enabled(path: Path, source_name: str, enabled: bool) -> bool:
    """Pause (False) or resume (True) a schedule. Returns True if found."""
    rows = load_schedules_json(path)
    clean = str(source_name or "").strip().lower()
    found = False
    for row in rows:
        if str(row.get("source_name") or "").strip().lower() == clean:
            row["enabled"] = bool(enabled)
            found = True
            break
    if found:
        save_schedules_json(path, rows)
    return found


def _validate_cron_expression(cron: str) -> bool:
    """Basic validation: must have exactly 5 non-empty segments."""
    parts = str(cron or "").strip().split()
    return len(parts) == 5 and all(p.strip() for p in parts)
