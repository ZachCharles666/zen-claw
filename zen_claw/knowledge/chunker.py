"""Text chunking utilities for RAG ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ChunkConfig:
    max_chars: int = 800
    overlap_chars: int = 80
    min_chars: int = 50
    use_jieba: bool = True


_ZH_SENT_RE = re.compile(r"(?<=[。！？；…\n])\s*")
_EN_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_PARA_RE = re.compile(r"\n\s*\n")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _is_cjk_dominant(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    cjk_count = sum(1 for c in chars if _CJK_RE.match(c))
    return cjk_count / len(chars) > 0.2


def _split_sentences(text: str, use_jieba: bool = True) -> list[str]:
    if _is_cjk_dominant(text):
        if use_jieba:
            try:
                import jieba  # noqa: F401
            except ImportError:
                pass
        sentences = _ZH_SENT_RE.split(text)
    else:
        sentences = _EN_SENT_RE.split(text)
    out: list[str] = []
    for sent in sentences:
        for part in _PARA_RE.split(sent):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _split_by_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARA_RE.split(text) if p.strip()]


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        stride = max(1, max_chars - max(0, overlap))
        start += stride
    return chunks


class TextChunker:
    def __init__(self, config: ChunkConfig | None = None):
        self._cfg = config or ChunkConfig()

    def chunk(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        cfg = self._cfg
        units: list[str] = []
        for para in _split_by_paragraphs(text.strip()):
            if len(para) <= cfg.max_chars:
                units.append(para)
            else:
                split = _split_sentences(para, use_jieba=cfg.use_jieba)
                units.extend(split if split else [para])

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for unit in units:
            if len(unit) > cfg.max_chars:
                if current:
                    merged = "\n".join(current).strip()
                    if len(merged) >= cfg.min_chars:
                        chunks.append(merged)
                    current, current_len = [], 0
                for part in _hard_split(unit, cfg.max_chars, cfg.overlap_chars):
                    if len(part) >= cfg.min_chars:
                        chunks.append(part)
                continue

            if current and current_len + 1 + len(unit) > cfg.max_chars:
                merged = "\n".join(current).strip()
                if len(merged) >= cfg.min_chars:
                    chunks.append(merged)
                overlap = merged[-cfg.overlap_chars:] if cfg.overlap_chars else ""
                current = [overlap, unit] if overlap else [unit]
                current_len = len(overlap) + len(unit) + (1 if overlap else 0)
            else:
                current.append(unit)
                current_len += len(unit) + (1 if len(current) > 1 else 0)

        if current:
            merged = "\n".join(current).strip()
            if len(merged) >= cfg.min_chars:
                chunks.append(merged)
        if not chunks and text.strip():
            # Keep very short documents as one chunk instead of dropping all content.
            chunks.append(text.strip())
        return chunks

    def chunk_with_metadata(self, text: str, source: str = "", page: int | None = None) -> list[dict]:
        chunks = self.chunk(text)
        return [
            {"content": c, "source": source, "page": page, "chunk_index": i}
            for i, c in enumerate(chunks)
        ]
