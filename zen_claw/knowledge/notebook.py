"""Notebook metadata management for knowledge base."""

from __future__ import annotations

import json
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notebook":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            created_at=str(data.get("created_at", "")),
            doc_count=int(data.get("doc_count", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NotebookManager:
    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "knowledge"
        self._root.mkdir(parents=True, exist_ok=True)
        self._index = self._root / "notebooks.json"

    def _load(self) -> dict[str, Any]:
        if not self._index.exists():
            return {"notebooks": []}
        try:
            return json.loads(self._index.read_text(encoding="utf-8"))
        except Exception:
            return {"notebooks": []}

    def _save(self, data: dict[str, Any]) -> None:
        self._index.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

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

    def create(self, name: str) -> Notebook:
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
        )
        data = self._load()
        data.setdefault("notebooks", []).append(nb.to_dict())
        self._save(data)
        return nb

    def get_or_create(self, name: str) -> Notebook:
        found = self.get(name)
        if found:
            return found
        return self.create(name)

    def bump_doc_count(self, notebook_id: str, delta: int) -> None:
        data = self._load()
        rows = data.get("notebooks", [])
        for row in rows:
            if str(row.get("id", "")).lower() == str(notebook_id).lower():
                row["doc_count"] = max(0, int(row.get("doc_count", 0)) + int(delta))
                break
        self._save(data)
