"""Rule data and helpers for the compliance_check business skill."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegexRule:
    pattern: str
    rule: str
    severity: str
    suggestion: str


DEFAULT_FINANCE_TERMS = [
    "保证收益",
    "稳赚不赔",
    "零风险",
    "保本保息",
    "最高收益",
    "收益翻倍",
]

DEFAULT_AD_TERMS = [
    "最",
    "第一",
    "唯一",
    "顶级",
    "国家级",
    "最佳",
]

DEFAULT_REGEX_RULES = [
    RegexRule(
        pattern=r"收益率\s*\d+(\.\d+)?%",
        rule="收益率宣传需人工复核",
        severity="medium",
        suggestion="补充明确条件、来源和风险提示",
    ),
    RegexRule(
        pattern=r"(保证|承诺).{0,8}(收益|回报)",
        rule="禁止承诺收益",
        severity="high",
        suggestion="改为描述历史表现或风险提示",
    ),
    RegexRule(
        pattern=r"(全网|行业|全国).{0,4}(第一|领先)",
        rule="绝对化排名表述需证据支持",
        severity="medium",
        suggestion="删除绝对化排名或补充权威依据",
    ),
]


def load_terms(path: Path, fallback: list[str]) -> list[str]:
    """Load line-based terms from disk, fallback to defaults when absent."""
    if not path.exists():
        return list(fallback)
    rows: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        rows.append(token)
    return rows or list(fallback)
