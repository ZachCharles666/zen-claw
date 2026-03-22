"""Notebook metadata management for knowledge base."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Notebook:
    id: str
    name: str
    created_at: str
    doc_count: int = 0
    store_backend: str = ""
    retention_max_documents: int = 0
    retention_max_age_days: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notebook":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            created_at=str(data.get("created_at", "")),
            doc_count=int(data.get("doc_count", 0)),
            store_backend=cls._normalize_store_backend(data.get("store_backend", "")),
            retention_max_documents=max(0, int(data.get("retention_max_documents", 0) or 0)),
            retention_max_age_days=max(0, int(data.get("retention_max_age_days", 0) or 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def _normalize_store_backend(value: Any) -> str:
        backend = str(value or "").strip().lower()
        if backend in {"chroma", "memory"}:
            return backend
        return ""


class NotebookManager:
    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "knowledge"
        self._root.mkdir(parents=True, exist_ok=True)
        self._index = self._root / "notebooks.json"
        self._sources_index = self._root / "document_sources.json"

    def _load(self) -> dict[str, Any]:
        if not self._index.exists():
            return {"notebooks": []}
        try:
            return json.loads(self._index.read_text(encoding="utf-8"))
        except Exception:
            return {"notebooks": []}

    def _save(self, data: dict[str, Any]) -> None:
        self._index.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_sources(self) -> dict[str, Any]:
        if not self._sources_index.exists():
            return {"notebooks": {}}
        try:
            return json.loads(self._sources_index.read_text(encoding="utf-8"))
        except Exception:
            return {"notebooks": {}}

    def _save_sources(self, data: dict[str, Any]) -> None:
        self._sources_index.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def list(self) -> list[Notebook]:
        raw = self._load().get("notebooks", [])
        rows = [Notebook.from_dict(x) for x in raw if isinstance(x, dict)]
        rows.sort(key=lambda x: x.name.lower())
        return rows

    def get(self, name_or_id: str) -> Notebook | None:
        key = str(name_or_id).strip().lower()
        for nb in self.list():
            if nb.id.lower() == key or nb.name.lower() == key:
                return nb
        return None

    def create(
        self,
        name: str,
        *,
        store_backend: str = "",
        retention_max_documents: int = 0,
        retention_max_age_days: int = 0,
    ) -> Notebook:
        clean = str(name).strip()
        if not clean:
            raise ValueError("notebook name cannot be empty")
        if self.get(clean):
            raise ValueError(f"notebook already exists: {clean}")
        nb = Notebook(
            id=clean.replace(" ", "_").lower(),
            name=clean,
            created_at=datetime.now(UTC).isoformat(),
            doc_count=0,
            store_backend=Notebook._normalize_store_backend(store_backend),
            retention_max_documents=max(0, int(retention_max_documents or 0)),
            retention_max_age_days=max(0, int(retention_max_age_days or 0)),
        )
        data = self._load()
        data.setdefault("notebooks", []).append(nb.to_dict())
        self._save(data)
        return nb

    def get_or_create(
        self,
        name: str,
        *,
        store_backend: str = "",
        retention_max_documents: int = 0,
        retention_max_age_days: int = 0,
    ) -> Notebook:
        found = self.get(name)
        if found:
            return found
        return self.create(
            name,
            store_backend=store_backend,
            retention_max_documents=retention_max_documents,
            retention_max_age_days=retention_max_age_days,
        )

    def set_store_backend(self, notebook_id: str, store_backend: str) -> Notebook:
        normalized_backend = Notebook._normalize_store_backend(store_backend)
        data = self._load()
        rows = data.get("notebooks", [])
        for row in rows:
            row_id = str(row.get("id", "")).strip().lower()
            row_name = str(row.get("name", "")).strip().lower()
            needle = str(notebook_id).strip().lower()
            if needle and (row_id == needle or row_name == needle):
                row["store_backend"] = normalized_backend
                self._save(data)
                return Notebook.from_dict(row)
        raise ValueError(f"notebook not found: {notebook_id}")

    def set_retention_policy(
        self,
        notebook_id: str,
        *,
        retention_max_documents: int | None = None,
        retention_max_age_days: int | None = None,
    ) -> Notebook:
        data = self._load()
        rows = data.get("notebooks", [])
        needle = str(notebook_id).strip().lower()
        for row in rows:
            row_id = str(row.get("id", "")).strip().lower()
            row_name = str(row.get("name", "")).strip().lower()
            if needle and (row_id == needle or row_name == needle):
                if retention_max_documents is not None:
                    row["retention_max_documents"] = max(0, int(retention_max_documents or 0))
                if retention_max_age_days is not None:
                    row["retention_max_age_days"] = max(0, int(retention_max_age_days or 0))
                self._save(data)
                return Notebook.from_dict(row)
        raise ValueError(f"notebook not found: {notebook_id}")

    def bump_doc_count(self, notebook_id: str, delta: int) -> None:
        data = self._load()
        rows = data.get("notebooks", [])
        for row in rows:
            if str(row.get("id", "")).lower() == str(notebook_id).lower():
                row["doc_count"] = max(0, int(row.get("doc_count", 0)) + int(delta))
                break
        self._save(data)

    def record_source(
        self,
        notebook_id: str,
        source: str,
        doc_units: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        clean_source = str(source).strip()
        if not clean_source or int(doc_units) <= 0:
            return ""
        normalized_metadata = dict(metadata or {})
        data = self._load_sources()
        notebooks = data.setdefault("notebooks", {})
        rows = notebooks.setdefault(str(notebook_id), {})
        for document_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            if str(row.get("source", "")).strip() == clean_source:
                row["doc_units"] = int(row.get("doc_units", 0)) + int(doc_units)
                if normalized_metadata:
                    row["metadata"] = normalized_metadata
                self._save_sources(data)
                return str(document_id)
        document_id = str(uuid.uuid4())
        rows[document_id] = {
            "source": clean_source,
            "doc_units": int(doc_units),
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": normalized_metadata,
        }
        self._save_sources(data)
        return document_id

    def list_documents(self, notebook_id: str) -> list[dict[str, Any]]:
        data = self._load_sources()
        notebooks = data.setdefault("notebooks", {})
        rows = notebooks.get(str(notebook_id))
        if not isinstance(rows, dict):
            return []
        out: list[dict[str, Any]] = []
        for document_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "document_id": str(document_id),
                    "source": str(row.get("source", "")),
                    "doc_units": int(row.get("doc_units", 0) or 0),
                    "created_at": str(row.get("created_at", "")),
                    "metadata": dict(row.get("metadata") or {}),
                }
            )
        out.sort(key=lambda item: (item["created_at"], item["source"], item["document_id"]))
        return out

    def remove_document(self, notebook_id: str, document_id: str) -> dict[str, Any]:
        clean_document_id = str(document_id).strip()
        if not clean_document_id:
            return {"document_id": "", "source": "", "doc_units": 0, "metadata": {}}
        data = self._load_sources()
        notebooks = data.setdefault("notebooks", {})
        rows = notebooks.get(str(notebook_id))
        if not isinstance(rows, dict):
            return {
                "document_id": clean_document_id,
                "source": "",
                "doc_units": 0,
                "metadata": {},
            }
        row = rows.pop(clean_document_id, None)
        if not rows:
            notebooks.pop(str(notebook_id), None)
        self._save_sources(data)
        if not isinstance(row, dict):
            return {
                "document_id": clean_document_id,
                "source": "",
                "doc_units": 0,
                "metadata": {},
            }
        return {
            "document_id": clean_document_id,
            "source": str(row.get("source", "")),
            "doc_units": max(0, int(row.get("doc_units", 0) or 0)),
            "metadata": dict(row.get("metadata") or {}),
        }

    def clear_documents(self, notebook_id: str) -> list[dict[str, Any]]:
        data = self._load_sources()
        notebooks = data.setdefault("notebooks", {})
        rows = notebooks.pop(str(notebook_id), None)
        self._save_sources(data)
        if not isinstance(rows, dict):
            return []
        out: list[dict[str, Any]] = []
        for document_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "document_id": str(document_id),
                    "source": str(row.get("source", "")),
                    "doc_units": max(0, int(row.get("doc_units", 0) or 0)),
                    "metadata": dict(row.get("metadata") or {}),
                }
            )
        return out
