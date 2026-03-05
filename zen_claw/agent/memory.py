"""Memory system for persistent agent memory."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from zen_claw.agent.memory_recall import KeywordRecallStrategy, MemoryRecallStrategy
from zen_claw.utils.helpers import ensure_dir, today_date


class MemoryStore:
    """
    Memory system for the agent.

    Supports daily notes (memory/YYYY-MM-DD.md) and long-term memory (MEMORY.md).
    """

    def __init__(self, workspace: Path, recall_strategy: MemoryRecallStrategy | None = None):
        self.workspace = workspace
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self._default_context_max_chars = 4000
        self._default_recent_max_chars = 1800
        self.recall_strategy = recall_strategy or KeywordRecallStrategy()

    def get_today_file(self) -> Path:
        """Get path to today's memory file."""
        return self.memory_dir / f"{today_date()}.md"

    def read_today(self) -> str:
        """Read today's memory notes."""
        today_file = self.get_today_file()
        if today_file.exists() and self._is_safe_memory_file(today_file):
            return today_file.read_text(encoding="utf-8")
        return ""

    def append_today(self, content: str) -> None:
        """Append content to today's memory notes."""
        today_file = self.get_today_file()
        self._ensure_safe_write_target(today_file)

        if today_file.exists():
            existing = today_file.read_text(encoding="utf-8")
            content = existing + "\n" + content
        else:
            # Add header for new day
            header = f"# {today_date()}\n\n"
            content = header + content

        today_file.write_text(content, encoding="utf-8")

    def read_long_term(self) -> str:
        """Read long-term memory (MEMORY.md)."""
        if self.memory_file.exists() and self._is_safe_memory_file(self.memory_file):
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        """Write to long-term memory (MEMORY.md)."""
        self._ensure_safe_write_target(self.memory_file)
        self.memory_file.write_text(content, encoding="utf-8")

    def read_tool_learning(self) -> str:
        """Read reflection-derived tool learning notes (TOOLS_LEARNING.md)."""
        path = self.memory_dir / "TOOLS_LEARNING.md"
        if path.exists() and self._is_safe_memory_file(path):
            return path.read_text(encoding="utf-8")
        return ""

    def get_recent_memories(self, days: int = 7, max_chars: int | None = None) -> str:
        """
        Get memories from the last N days.

        Args:
            days: Number of days to look back.

        Returns:
            Combined memory content.
        """
        from datetime import timedelta

        if days <= 0:
            return ""

        memories = []
        budget = (
            max_chars
            if isinstance(max_chars, int) and max_chars > 0
            else self._default_recent_max_chars
        )
        today = datetime.now().date()

        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            file_path = self.memory_dir / f"{date_str}.md"

            if file_path.exists() and self._is_safe_memory_file(file_path):
                content = file_path.read_text(encoding="utf-8")
                if len(content) > budget:
                    content = content[:budget]
                memories.append(content)
                budget -= len(content)
                if budget <= 0:
                    break

        return "\n\n---\n\n".join(memories)

    def list_memory_files(self) -> list[Path]:
        """List all memory files sorted by date (newest first)."""
        if not self.memory_dir.exists():
            return []

        files = [f for f in self.memory_dir.glob("????-??-??.md") if self._is_safe_memory_file(f)]
        return sorted(files, reverse=True)

    def get_memory_context(self, include_recent_days: int = 3, max_chars: int | None = None) -> str:
        """
        Get memory context for the agent.

        Returns:
            Formatted memory context including long-term and recent memories.
        """
        parts = []
        budget = (
            max_chars
            if isinstance(max_chars, int) and max_chars > 0
            else self._default_context_max_chars
        )

        # Long-term memory
        long_term = self.read_long_term()
        if long_term:
            if len(long_term) > budget:
                long_term = long_term[:budget]
            parts.append("## Long-term Memory\n" + long_term)
            budget -= len(long_term)

        # Today's notes
        today = self.read_today()
        if today and budget > 0:
            if len(today) > budget:
                today = today[:budget]
            parts.append("## Today's Notes\n" + today)
            budget -= len(today)

        # Recent notes (excluding today) for better session continuity.
        if include_recent_days > 1 and budget > 0:
            recent = self.get_recent_memories(days=include_recent_days, max_chars=budget)
            today_text = self.read_today()
            if recent and today_text:
                recent = recent.replace(today_text, "", 1).strip()
            if recent:
                parts.append("## Recent Notes\n" + recent)

        return "\n\n".join(parts) if parts else ""

    def get_relevant_memory_context(
        self,
        query: str,
        days: int = 7,
        max_items: int = 8,
        max_chars: int = 1200,
    ) -> str:
        """
        Retrieve query-relevant memory snippets using pluggable recall strategy.
        """
        query = (query or "").strip()
        if not query:
            return ""

        candidates: list[tuple[str, str]] = []

        long_term = self.read_long_term()
        if long_term:
            for line in long_term.splitlines():
                text = line.strip().lstrip("-").strip()
                if text:
                    candidates.append(("long_term", text))

        recent = self.get_recent_memories(days=days, max_chars=max(2000, max_chars * 3))
        if recent:
            for line in recent.splitlines():
                text = line.strip().lstrip("-").strip()
                if text and not text.startswith("#"):
                    candidates.append(("recent", text))

        scored: list[tuple[float, str]] = []
        if hasattr(self.recall_strategy, "bulk_score"):
            candidates_only = [c for _, c in candidates]
            scored_bulk = self.recall_strategy.bulk_score(query, candidates_only)
            source_map = {c: s for s, c in candidates}
            for score, c in scored_bulk:
                if source_map.get(c) == "recent":
                    score += 0.05
                scored.append((score, c))
        else:
            for source, c in candidates:
                score = self.recall_strategy.score(query, c)
                if score > 0:
                    if source == "recent":
                        score += 0.05
                    scored.append((score, c))

        if not scored:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)
        selected: list[str] = []
        seen: set[str] = set()
        total = 0
        for _, text in scored:
            if len(selected) >= max_items:
                break
            norm = " ".join(text.split()).strip().lower()
            if not norm or norm in seen:
                continue
            if total + len(text) > max_chars:
                continue
            seen.add(norm)
            selected.append(text)
            total += len(text)

        if not selected:
            return ""
        return "## Relevant Memory\n" + "\n".join(f"- {s}" for s in selected)

    def get_recent_memory_context(
        self,
        days: int = 3,
        max_items: int = 8,
        max_chars: int = 1200,
    ) -> str:
        """Build a concise recent-memory block without query ranking."""
        recent = self.get_recent_memories(days=days, max_chars=max(2000, max_chars * 3))
        if not recent:
            return ""

        selected: list[str] = []
        seen: set[str] = set()
        total = 0
        for line in recent.splitlines():
            text = line.strip().lstrip("-").strip()
            if not text or text.startswith("#"):
                continue
            norm = " ".join(text.split()).strip().lower()
            if not norm or norm in seen:
                continue
            if len(selected) >= max_items:
                break
            if total + len(text) > max_chars:
                continue
            seen.add(norm)
            selected.append(text)
            total += len(text)

        if not selected:
            return ""
        return "## Recent Memory\n" + "\n".join(f"- {s}" for s in selected)

    def get_tool_learning_context(
        self, max_chars: int = 900, query: str | None = None, max_items: int = 8
    ) -> str:
        """Return bounded tool-learning notes for prompt guidance."""
        text = self.read_tool_learning().strip()
        if not text:
            return ""

        entries = self._parse_tool_learning_entries(text)
        if not entries:
            if max_chars > 0 and len(text) > max_chars:
                text = text[:max_chars]
            return "## Tool Learning\n" + text

        ranked = sorted(
            entries,
            key=lambda e: self._score_tool_learning_entry(e, query=query),
            reverse=True,
        )
        selected_lines: list[str] = []
        total = 0
        for item in ranked:
            if len(selected_lines) >= max_items:
                break
            line = item.get("raw", "")
            if not line:
                continue
            if max_chars > 0 and total + len(line) > max_chars:
                continue
            selected_lines.append(line)
            total += len(line)
        if not selected_lines:
            return ""
        return "## Tool Learning\n" + "\n".join(selected_lines)

    def _parse_tool_learning_entries(self, text: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("- "):
                continue
            if (
                " tool=" not in line
                or " sig=" not in line
                or " from=" not in line
                or " to=" not in line
            ):
                continue
            try:
                prefix, after_error = line.split(" error=", 1)
                error_part, after_from = after_error.split(" from=", 1)
                from_part, after_to = after_from.split(" to=", 1)
                if " trace_id=" in after_to:
                    to_part, trace_part = after_to.split(" trace_id=", 1)
                else:
                    to_part, trace_part = after_to, ""
                tool = prefix.split(" tool=", 1)[1].split(" sig=", 1)[0].strip()
                sig = prefix.split(" sig=", 1)[1].strip()
                error_message = (
                    json.loads(error_part.strip())
                    if error_part.strip().startswith('"')
                    else error_part.strip()
                )
                failed_args = json.loads(from_part.strip())
                corrected_args = json.loads(to_part.strip())
                if not isinstance(failed_args, dict) or not isinstance(corrected_args, dict):
                    continue
                entries.append(
                    {
                        "raw": line,
                        "tool": tool,
                        "sig": sig,
                        "error": str(error_message),
                        "from": failed_args,
                        "to": corrected_args,
                        "trace_id": trace_part.strip(),
                    }
                )
            except Exception:
                continue
        return entries

    def _score_tool_learning_entry(self, entry: dict[str, Any], query: str | None) -> float:
        score = 0.05  # keep deterministic positive ordering
        q = (query or "").strip().lower()
        if not q:
            return score
        tokens = set(re.findall(r"[a-z0-9_]+", q))
        if not tokens:
            return score

        tool = str(entry.get("tool") or "").strip().lower()
        error_text = str(entry.get("error") or "").lower()
        from_args = str(json.dumps(entry.get("from", {}), ensure_ascii=False)).lower()
        to_args = str(json.dumps(entry.get("to", {}), ensure_ascii=False)).lower()
        if tool and tool in tokens:
            score += 2.0

        for token in tokens:
            if len(token) <= 1:
                continue
            if token in error_text:
                score += 0.8
            if token in from_args:
                score += 0.9
            if token in to_args:
                score += 1.0
        return score

    def suggest_tool_arg_rewrite(
        self, tool_name: str, args: dict[str, Any], query: str | None = None
    ) -> dict[str, Any] | None:
        """
        Suggest rewritten args from learned correction patterns.

        Conservative policy:
        - only consider entries for the same tool
        - only apply when learned `from` args exactly match current args
        """
        text = self.read_tool_learning().strip()
        if not text:
            return None
        entries = self._parse_tool_learning_entries(text)
        if not entries:
            return None

        candidates: list[dict[str, Any]] = []
        for entry in entries:
            if str(entry.get("tool") or "").strip().lower() != tool_name.strip().lower():
                continue
            learned_from = entry.get("from")
            learned_to = entry.get("to")
            if not isinstance(learned_from, dict) or not isinstance(learned_to, dict):
                continue
            if learned_from == args:
                candidates.append(entry)
        if not candidates:
            return None
        best = max(candidates, key=lambda e: self._score_tool_learning_entry(e, query=query))
        rewrite = best.get("to")
        if isinstance(rewrite, dict):
            return dict(rewrite)
        return None

    def _is_safe_memory_file(self, file_path: Path) -> bool:
        """Prevent following links or traversing outside memory directory."""
        try:
            resolved_file = file_path.resolve()
            resolved_memory = self.memory_dir.resolve()
            resolved_file.relative_to(resolved_memory)
            return resolved_file.is_file() and not file_path.is_symlink()
        except Exception:
            return False

    def _ensure_safe_write_target(self, file_path: Path) -> None:
        """Reject writes to symlinked or escaped paths."""
        try:
            parent = file_path.parent.resolve()
            memory_root = self.memory_dir.resolve()
            parent.relative_to(memory_root)
            if file_path.exists() and file_path.is_symlink():
                raise PermissionError(f"Refuse to write through symlink: {file_path}")
        except ValueError:
            raise PermissionError(f"Refuse to write outside memory dir: {file_path}") from None
