"""Token usage tracking and reporting for A1."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TokenUsageRecord:
    """Single token usage record for one LLM call."""

    at_ms: int
    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    session_id: str = ""
    intent: str = ""

    @property
    def date_str(self) -> str:
        """YYYY-MM-DD in UTC."""
        return datetime.fromtimestamp(self.at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenUsageRecord":
        return cls(
            at_ms=int(d.get("at_ms") or 0),
            agent_id=str(d.get("agent_id") or ""),
            model=str(d.get("model") or ""),
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
            total_tokens=int(d.get("total_tokens") or 0),
            session_id=str(d.get("session_id") or ""),
            intent=str(d.get("intent") or ""),
        )


class TokenUsageTracker:
    """
    Records token usage per LLM call and provides summary reports.

    Storage: JSONL file at {data_dir}/dashboard/token_usage.log.jsonl
    Each line = one TokenUsageRecord serialized as JSON.
    """

    LOG_FILENAME = "token_usage.log.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._log_path = Path(data_dir) / "dashboard" / self.LOG_FILENAME

    def record(
        self,
        *,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str = "",
        intent: str = "",
        at_ms: int | None = None,
    ) -> TokenUsageRecord:
        """Append a usage record to the JSONL log and return it."""
        rec = TokenUsageRecord(
            at_ms=at_ms if at_ms is not None else int(time.time() * 1000),
            agent_id=agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            session_id=session_id,
            intent=intent,
        )
        self._append(rec)
        return rec

    def _append(self, rec: TokenUsageRecord) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def read_all(self, *, limit: int = 10000) -> list[TokenUsageRecord]:
        """Read all records from log (most recent `limit`)."""
        if not self._log_path.exists():
            return []
        records: list[TokenUsageRecord] = []
        try:
            for line in self._log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if isinstance(d, dict):
                        records.append(TokenUsageRecord.from_dict(d))
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue  # skip corrupted lines
        except OSError:
            return []
        return records[-limit:]

    def daily_total(self, agent_id: str, date_str: str | None = None) -> int:
        """Total tokens consumed by an agent on a given UTC date (default: today)."""
        today = date_str or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return sum(
            r.total_tokens
            for r in self.read_all()
            if r.agent_id == agent_id and r.date_str == today
        )

    def is_over_budget(self, agent_id: str, budget_daily: int, date_str: str | None = None) -> bool:
        """Return True when daily token total exceeds the configured budget."""
        if budget_daily <= 0:
            return False  # 0 means unlimited
        return self.daily_total(agent_id, date_str) >= budget_daily

    def summary(
        self,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Aggregate token usage grouped by agent, model, and day.

        Returns:
            {
              "total": {"input": N, "output": N, "total": N, "calls": N},
              "by_agent": {"agent_id": {"input": N, "output": N, "total": N, "calls": N}},
              "by_model": {"model": {"input": N, "output": N, "total": N, "calls": N}},
              "by_day":   {"YYYY-MM-DD": {"input": N, "output": N, "total": N, "calls": N}},
            }
        """
        records = self.read_all()
        if since_ms is not None:
            records = [r for r in records if r.at_ms >= since_ms]
        if until_ms is not None:
            records = [r for r in records if r.at_ms <= until_ms]

        def _empty() -> dict[str, int]:
            return {"input": 0, "output": 0, "total": 0, "calls": 0}

        total = _empty()
        by_agent: dict[str, dict[str, int]] = defaultdict(lambda: _empty().copy())
        by_model: dict[str, dict[str, int]] = defaultdict(lambda: _empty().copy())
        by_day: dict[str, dict[str, int]] = defaultdict(lambda: _empty().copy())

        for r in records:
            for bucket in (total, by_agent[r.agent_id], by_model[r.model], by_day[r.date_str]):
                bucket["input"] += r.input_tokens
                bucket["output"] += r.output_tokens
                bucket["total"] += r.total_tokens
                bucket["calls"] += 1

        return {
            "total": total,
            "by_agent": dict(by_agent),
            "by_model": dict(by_model),
            "by_day": dict(by_day),
        }
