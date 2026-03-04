"""LLM-assisted memory extraction for long-term and daily notes."""

import json
from dataclasses import dataclass


@dataclass
class ExtractedMemory:
    """Parsed memory extraction result."""

    should_write: bool
    memory_type: str
    content: str


class MemoryExtractor:
    """Extract durable memories from a completed turn."""

    def __init__(self, max_chars: int = 300, min_turn_chars: int = 24):
        self.max_chars = max_chars
        self.min_turn_chars = min_turn_chars

    def should_extract(self, user_text: str, assistant_text: str) -> bool:
        total = len(user_text.strip()) + len(assistant_text.strip())
        return total >= self.min_turn_chars

    def build_prompt(self, user_text: str, assistant_text: str, memory_snapshot: str) -> str:
        return (
            "You are deciding whether this turn contains durable memory worth storing.\n"
            "Return JSON only with keys:\n"
            "- should_write (boolean)\n"
            "- memory_type ('long_term' or 'daily')\n"
            "- content (string, <= 300 chars)\n\n"
            "Write memory only if it is likely useful in future sessions.\n"
            "Avoid transient details and avoid duplicates of existing memory.\n\n"
            "Existing memory excerpt:\n"
            f"{memory_snapshot[:1200] or '(empty)'}\n\n"
            "Turn:\n"
            f"user: {user_text[:1000]}\n"
            f"assistant: {assistant_text[:1000]}"
        )

    def parse(self, raw: str | None) -> ExtractedMemory:
        if not raw:
            return ExtractedMemory(False, "daily", "")

        data = self._extract_json(raw)
        if not isinstance(data, dict):
            return ExtractedMemory(False, "daily", "")

        should_write = bool(data.get("should_write", False))
        memory_type = str(data.get("memory_type", "daily")).strip().lower()
        if memory_type not in {"long_term", "daily"}:
            memory_type = "daily"
        content = str(data.get("content", "")).strip()[: self.max_chars]
        if not content:
            should_write = False
        return ExtractedMemory(should_write, memory_type, content)

    def _extract_json(self, raw: str):
        try:
            return json.loads(raw)
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                return None
        return None
