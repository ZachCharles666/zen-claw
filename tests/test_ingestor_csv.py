"""Tests for CSV ingestion in Ingestor."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from zen_claw.knowledge.ingestor import Ingestor


def _write_csv(path: Path, rows: list[dict], encoding: str = "utf-8") -> None:
    if not rows:
        path.write_text("", encoding=encoding)
        return
    with path.open("w", newline="", encoding=encoding) as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class TestIngestorCSV:
    @pytest.mark.asyncio
    async def test_basic_utf8_csv(self, tmp_path: Path) -> None:
        """Standard UTF-8 CSV rows are parsed into Documents."""
        csv_path = tmp_path / "products.csv"
        _write_csv(
            csv_path,
            [
                {"name": "理财A", "category": "固收", "yield": "3.5%"},
                {"name": "理财B", "category": "混合", "yield": "5.0%"},
            ],
        )
        ingestor = Ingestor()
        docs = await ingestor.ingest(str(csv_path))

        assert len(docs) == 2
        assert "name: 理财A" in docs[0].content
        assert "category: 固收" in docs[0].content
        assert "yield: 3.5%" in docs[0].content

    @pytest.mark.asyncio
    async def test_source_format(self, tmp_path: Path) -> None:
        """Each Document source should follow filename:rowN pattern."""
        csv_path = tmp_path / "data.csv"
        _write_csv(csv_path, [{"a": "1"}, {"a": "2"}, {"a": "3"}])

        docs = await Ingestor().ingest(str(csv_path))

        assert docs[0].source == "data.csv:row1"
        assert docs[1].source == "data.csv:row2"
        assert docs[2].source == "data.csv:row3"

    @pytest.mark.asyncio
    async def test_page_equals_row_number(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "rows.csv"
        _write_csv(csv_path, [{"x": str(i)} for i in range(1, 4)])

        docs = await Ingestor().ingest(str(csv_path))

        assert [d.page for d in docs] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_metadata_contains_row_columns(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "meta.csv"
        _write_csv(csv_path, [{"product": "X", "risk": "low"}])

        docs = await Ingestor().ingest(str(csv_path))

        assert docs[0].metadata.get("product") == "X"
        assert docs[0].metadata.get("risk") == "low"

    @pytest.mark.asyncio
    async def test_empty_cells_excluded_from_content(self, tmp_path: Path) -> None:
        """Empty cells should not appear in document content."""
        csv_path = tmp_path / "sparse.csv"
        _write_csv(csv_path, [{"name": "产品A", "note": "", "score": "90"}])

        docs = await Ingestor().ingest(str(csv_path))

        assert "note" not in docs[0].content
        assert "score: 90" in docs[0].content

    @pytest.mark.asyncio
    async def test_all_empty_row_skipped(self, tmp_path: Path) -> None:
        """Rows where all cells are empty should be skipped."""
        csv_path = tmp_path / "empty_row.csv"
        _write_csv(csv_path, [{"a": "val"}, {"a": ""}, {"a": "val2"}])

        docs = await Ingestor().ingest(str(csv_path))

        # Row 2 (all empty) is skipped → 2 docs
        assert len(docs) == 2
        contents = [d.content for d in docs]
        assert any("val2" in c for c in contents)

    @pytest.mark.asyncio
    async def test_gbk_csv_decoded(self, tmp_path: Path) -> None:
        """GBK-encoded CSV (common for Chinese data exports) should be decoded."""
        csv_path = tmp_path / "gbk_data.csv"
        _write_csv(
            csv_path,
            [{"产品名称": "稳健理财", "风险等级": "低风险"}],
            encoding="gbk",
        )

        docs = await Ingestor().ingest(str(csv_path))

        assert len(docs) == 1
        assert "稳健理财" in docs[0].content

    @pytest.mark.asyncio
    async def test_csv_in_directory_ingest(self, tmp_path: Path) -> None:
        """CSV files inside a directory should be picked up by directory ingest."""
        (tmp_path / "a.csv").write_text("col\nvalue1\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("plain text", encoding="utf-8")

        docs = await Ingestor().ingest(str(tmp_path))

        sources = [d.source for d in docs]
        assert any("a.csv" in s for s in sources)
        assert any("b.txt" in s for s in sources)

    def test_csv_in_supported_suffixes(self) -> None:
        assert ".csv" in Ingestor.SUPPORTED_SUFFIXES

    @pytest.mark.asyncio
    async def test_unsupported_type_still_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "file.xyz"
        bad.write_text("data")

        with pytest.raises(ValueError, match="Unsupported file type"):
            await Ingestor().ingest(str(bad))
