"""Deterministic compliance precheck with optional semantic review."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zen_claw.providers.base import LLMProvider
from zen_claw.skills.compliance_check.rules import (
    DEFAULT_AD_TERMS,
    DEFAULT_FINANCE_TERMS,
    DEFAULT_REGEX_RULES,
    load_terms,
)


@dataclass(frozen=True)
class ComplianceIssue:
    text: str
    position: tuple[int, int]
    rule: str
    severity: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "position": [self.position[0], self.position[1]],
            "rule": self.rule,
            "severity": self.severity,
            "suggestion": self.suggestion,
        }


class ComplianceChecker:
    """Lightweight rules engine for compliance-sensitive copy review."""

    def __init__(
        self,
        dictionary_dir: Path | None = None,
        *,
        provider: LLMProvider | None = None,
    ):
        base_dir = dictionary_dir or (Path(__file__).parent / "dictionaries")
        self._finance_terms = load_terms(base_dir / "finance_sensitive.txt", DEFAULT_FINANCE_TERMS)
        self._ad_terms = load_terms(base_dir / "ad_law_forbidden.txt", DEFAULT_AD_TERMS)
        self._provider = provider

    def check_text(self, text: str, *, industry: str = "", channel: str = "") -> dict[str, Any]:
        text = text or ""
        issues = self._collect_issues(text, industry=industry, channel=channel)
        score = self._score(issues)
        return {
            "score": score,
            "risk_level": self._risk_level(score, issues),
            "issues": [item.to_dict() for item in issues],
            "review_notes": self._build_review_notes(issues, industry=industry, channel=channel),
        }

    async def review_text(
        self,
        text: str,
        *,
        industry: str = "",
        channel: str = "",
        policy_context: str = "",
    ) -> dict[str, Any]:
        """Run deterministic checks and optionally layer semantic review on top."""

        result = self.check_text(text, industry=industry, channel=channel)
        result["semantic_review"] = await self._semantic_review(
            text,
            industry=industry,
            channel=channel,
            policy_context=policy_context,
            base_result=result,
        )
        return result

    def _collect_issues(
        self, text: str, *, industry: str = "", channel: str = ""
    ) -> list[ComplianceIssue]:
        issues: list[ComplianceIssue] = []
        seen: set[tuple[int, int, str]] = set()

        def _append(issue: ComplianceIssue) -> None:
            key = (issue.position[0], issue.position[1], issue.rule)
            if key in seen:
                return
            seen.add(key)
            issues.append(issue)

        for term in self._finance_terms:
            for start in self._find_all(text, term):
                _append(
                    ComplianceIssue(
                        text=term,
                        position=(start, start + len(term)),
                        rule="金融敏感收益表述",
                        severity="high",
                        suggestion="改为风险揭示或中性收益描述",
                    )
                )

        for term in self._ad_terms:
            if len(term) <= 1:
                continue
            for start in self._find_all(text, term):
                _append(
                    ComplianceIssue(
                        text=term,
                        position=(start, start + len(term)),
                        rule="广告法绝对化用语",
                        severity="medium",
                        suggestion="替换为可证明的客观描述",
                    )
                )

        for rule in DEFAULT_REGEX_RULES:
            for match in re.finditer(rule.pattern, text, flags=re.IGNORECASE):
                _append(
                    ComplianceIssue(
                        text=match.group(0),
                        position=(match.start(), match.end()),
                        rule=rule.rule,
                        severity=rule.severity,
                        suggestion=rule.suggestion,
                    )
                )

        if industry.strip().lower() in {"finance", "banking", "insurance"} and "收益" in text:
            _append(
                ComplianceIssue(
                    text="收益",
                    position=(text.index("收益"), text.index("收益") + 2),
                    rule="金融宣传中的收益表述需人工复核",
                    severity="medium",
                    suggestion="补充适当性、风险和适用条件",
                )
            )
        if channel.strip().lower() in {"douyin", "xiaohongshu", "wechat_moments"} and (
            "限时" in text or "立刻下单" in text
        ):
            token = "限时" if "限时" in text else "立刻下单"
            _append(
                ComplianceIssue(
                    text=token,
                    position=(text.index(token), text.index(token) + len(token)),
                    rule="强刺激营销表述需复核",
                    severity="low",
                    suggestion="补充活动条件并避免误导性催促",
                )
            )

        return sorted(issues, key=lambda item: (item.position[0], item.position[1], item.rule))

    @staticmethod
    def _find_all(text: str, token: str) -> list[int]:
        if not token:
            return []
        starts: list[int] = []
        offset = 0
        while True:
            idx = text.find(token, offset)
            if idx < 0:
                return starts
            starts.append(idx)
            offset = idx + len(token)

    @staticmethod
    def _score(issues: list[ComplianceIssue]) -> int:
        penalty = 0
        for item in issues:
            if item.severity == "high":
                penalty += 25
            elif item.severity == "medium":
                penalty += 12
            else:
                penalty += 6
        return max(0, 100 - penalty)

    @staticmethod
    def _risk_level(score: int, issues: list[ComplianceIssue]) -> str:
        severities = {item.severity for item in issues}
        if "high" in severities or score <= 60:
            return "high"
        if "medium" in severities or score <= 85:
            return "medium"
        return "low"

    @staticmethod
    def _build_review_notes(
        issues: list[ComplianceIssue], *, industry: str = "", channel: str = ""
    ) -> list[str]:
        notes: list[str] = []
        if issues:
            notes.append("命中规则仅代表预检结果，发布前仍需人工确认。")
        if industry.strip().lower() in {"finance", "banking", "insurance"}:
            notes.append("金融行业文案建议补充风险揭示、适当性边界和数据来源。")
        if channel.strip():
            notes.append(f"渠道 `{channel}` 的最终发布规范仍需按业务侧规则复核。")
        return notes

    async def _semantic_review(
        self,
        text: str,
        *,
        industry: str,
        channel: str,
        policy_context: str,
        base_result: dict[str, Any],
    ) -> dict[str, Any]:
        base_level = str(base_result.get("risk_level") or "low")
        issues = base_result.get("issues") or []
        if self._provider is None:
            return {
                "status": "not_available",
                "reviewer": "deterministic_only",
                "risk_level": base_level,
                "rewrite_needed": base_level != "low",
                "confidence": "rule_only",
                "concerns": [
                    "未配置语义审查模型；当前结果仅基于规则预检。"
                ],
                "recommended_actions": self._semantic_actions(base_level, issue_count=len(issues)),
            }

        prompt = self._build_semantic_prompt(
            text=text,
            industry=industry,
            channel=channel,
            policy_context=policy_context,
            base_result=base_result,
        )
        response = await self._provider.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You review marketing compliance risk. "
                        "Return strict JSON with keys: risk_level, rewrite_needed, "
                        "confidence, concerns, recommended_actions, rationale."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        parsed = self._parse_semantic_payload(response.content)
        if parsed is None:
            return {
                "status": "invalid_response",
                "reviewer": "llm_semantic",
                "risk_level": base_level,
                "rewrite_needed": base_level != "low",
                "confidence": "low",
                "concerns": [
                    "语义审查模型未返回可解析 JSON；已回退到规则预检结论。"
                ],
                "recommended_actions": self._semantic_actions(base_level, issue_count=len(issues)),
                "raw_response": (response.content or "").strip(),
            }

        normalized_level = self._normalize_risk_level(parsed.get("risk_level"), fallback=base_level)
        rewrite_needed = bool(parsed.get("rewrite_needed", normalized_level != "low"))
        return {
            "status": "ok",
            "reviewer": "llm_semantic",
            "risk_level": normalized_level,
            "rewrite_needed": rewrite_needed,
            "confidence": self._normalize_confidence(parsed.get("confidence")),
            "concerns": self._normalize_string_list(parsed.get("concerns")),
            "recommended_actions": self._normalize_string_list(
                parsed.get("recommended_actions")
            )
            or self._semantic_actions(normalized_level, issue_count=len(issues)),
            "rationale": str(parsed.get("rationale") or "").strip(),
        }

    @staticmethod
    def _build_semantic_prompt(
        *,
        text: str,
        industry: str,
        channel: str,
        policy_context: str,
        base_result: dict[str, Any],
    ) -> str:
        issue_lines = "\n".join(
            f"- {item['rule']} | {item['severity']} | {item['text']}"
            for item in (base_result.get("issues") or [])
        )
        return (
            f"Industry: {industry or 'general'}\n"
            f"Channel: {channel or 'generic'}\n"
            f"Policy context: {policy_context or 'none'}\n"
            f"Deterministic risk level: {base_result.get('risk_level', 'low')}\n"
            f"Deterministic score: {base_result.get('score', 100)}\n"
            f"Deterministic issues:\n{issue_lines or '- none'}\n\n"
            f"Copy under review:\n{text}\n\n"
            "Return JSON only."
        )

    @staticmethod
    def _parse_semantic_payload(raw: str | None) -> dict[str, Any] | None:
        body = (raw or "").strip()
        if not body:
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{")
            end = body.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                data = json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _normalize_risk_level(value: Any, *, fallback: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"low", "medium", "high"}:
            return normalized
        return fallback

    @staticmethod
    def _normalize_confidence(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"low", "medium", "high", "rule_only"}:
            return normalized
        return "medium"

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _semantic_actions(risk_level: str, *, issue_count: int) -> list[str]:
        if risk_level == "high":
            return ["先重写高风险表述，再提交人工法务或合规复核。"]
        if risk_level == "medium":
            return ["补充限制条件、证据来源和风险提示后再发布。"]
        if issue_count > 0:
            return ["处理命中规则后再做一次人工抽检。"]
        return ["保留现有中性表述，并在发布前做常规人工复核。"]
