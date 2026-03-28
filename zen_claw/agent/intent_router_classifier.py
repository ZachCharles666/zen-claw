"""Lightweight Gate 2 classifier for delegated intent routes."""

from __future__ import annotations

import json
import re
from typing import Any

from zen_claw.agent.intent_router_contracts import IntentArbitrationResult
from zen_claw.providers.base import LLMProvider


class IntentRouterClassifier:
    """Run a lightweight LLM classification pass for delegated routes."""

    _JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

    async def classify(
        self,
        *,
        provider: LLMProvider,
        model: str,
        content: str,
        history: list[dict[str, Any]] | None = None,
        route_candidate: dict[str, Any] | None = None,
        delegate_reason: str | None = None,
        available_skills: list[str] | None = None,
    ) -> IntentArbitrationResult:
        messages = self._build_messages(
            content=content,
            history=history,
            route_candidate=route_candidate,
            delegate_reason=delegate_reason,
            available_skills=available_skills,
        )
        try:
            response = await provider.chat(
                messages=messages,
                tools=None,
                model=model,
                max_tokens=256,
                temperature=0.0,
            )
        except Exception as exc:
            return IntentArbitrationResult(kind="unclassified", diagnostic=f"classifier_error:{exc}")

        if response.finish_reason == "error":
            return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_finish_error")

        return self._parse_response(
            response.content,
            route_candidate=route_candidate,
            available_skills=available_skills,
        )

    @staticmethod
    def _build_messages(
        *,
        content: str,
        history: list[dict[str, Any]] | None,
        route_candidate: dict[str, Any] | None,
        delegate_reason: str | None,
        available_skills: list[str] | None,
    ) -> list[dict[str, str]]:
        candidate_name = ""
        candidate_source = ""
        if isinstance(route_candidate, dict):
            candidate_name = str(route_candidate.get("match") or "").strip()
            candidate_source = str(route_candidate.get("source") or "").strip()
        history_lines: list[str] = []
        for item in (history or [])[-4:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            text = str(item.get("content") or "").strip()
            if role and text:
                history_lines.append(f"{role}: {text}")
        payload = {
            "request": content,
            "delegate_reason": str(delegate_reason or ""),
            "route_candidate": {
                "match": candidate_name,
                "source": candidate_source,
            },
            "available_skills": [str(skill).strip() for skill in (available_skills or []) if str(skill).strip()],
            "recent_history": history_lines,
        }
        examples = [
            {
                "input": {"request": "帮我查一下上海明天的天气", "route_candidate": {"match": "weather", "source": "declarative"}},
                "output": {"kind": "confirm_candidate", "candidate_name": "weather", "diagnostic": "candidate_covers_request"},
            },
            {
                "input": {"request": "帮我查一下那个人发来的邮件", "route_candidate": {"match": "", "source": ""}},
                "output": {
                    "kind": "request_clarification",
                    "clarification_question": "你要查的是哪位发件人，或者是哪封邮件？",
                    "diagnostic": "missing_key_object",
                },
            },
            {
                "input": {"request": "把这段内容做成合规检查摘要", "route_candidate": {"match": "", "source": ""}},
                "output": {"kind": "select_skill", "skill_name": "compliance_check", "diagnostic": "known_skill_best_fit"},
            },
            {
                "input": {"request": "帮我比较这两个方案的取舍并给一个执行建议", "route_candidate": {"match": "", "source": ""}},
                "output": {"kind": "unclassified", "diagnostic": "complete_but_not_router_covered"},
            },
        ]
        return [
            {
                "role": "system",
                "content": (
                    "You are Gate 2 of an intent routing system. "
                    "Classify the delegated request. "
                    "Return JSON only with keys: kind, candidate_name, skill_name, clarification_question, diagnostic. "
                    "Allowed kind values: confirm_candidate, select_skill, request_clarification, unclassified. "
                    "Do not plan steps. Do not call tools. Do not answer the user request directly. "
                    "Use request_clarification only when the user request is missing a key object, target, or action and cannot be executed safely. "
                    "If the request is complete but not covered by the current router candidate set, return unclassified instead of asking a clarification. "
                    "Prefer confirm_candidate when the route candidate already covers the full request. "
                    "Prefer select_skill only when an available skill is a clearly better bounded fit."
                ),
            },
            {
                "role": "system",
                "content": json.dumps({"examples": examples}, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]

    def _parse_response(
        self,
        content: str | None,
        *,
        route_candidate: dict[str, Any] | None,
        available_skills: list[str] | None,
    ) -> IntentArbitrationResult:
        data = self._extract_json_object(content)
        if not isinstance(data, dict):
            return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_invalid_payload")

        kind = str(data.get("kind") or "").strip().lower()
        candidate_name = str(data.get("candidate_name") or "").strip() or None
        skill_name = str(data.get("skill_name") or "").strip() or None
        clarification_question = str(data.get("clarification_question") or "").strip() or None
        diagnostic = str(data.get("diagnostic") or "").strip() or None

        if kind not in {
            "confirm_candidate",
            "select_skill",
            "request_clarification",
            "unclassified",
        }:
            return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_unknown_kind")

        candidate_match = (
            str(route_candidate.get("match") or "").strip() if isinstance(route_candidate, dict) else ""
        )
        skill_set = {str(skill).strip() for skill in (available_skills or []) if str(skill).strip()}

        if kind == "confirm_candidate":
            if not candidate_match:
                return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_no_candidate")
            if candidate_name and candidate_name != candidate_match:
                return IntentArbitrationResult(
                    kind="unclassified",
                    diagnostic="classifier_candidate_mismatch",
                )
            return IntentArbitrationResult(
                kind="confirm_candidate",
                candidate_name=candidate_match,
                diagnostic=diagnostic,
            )

        if kind == "select_skill":
            if not skill_name:
                return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_missing_skill")
            if skill_set and skill_name not in skill_set:
                return IntentArbitrationResult(kind="unclassified", diagnostic="classifier_unknown_skill")
            return IntentArbitrationResult(
                kind="select_skill",
                skill_name=skill_name,
                diagnostic=diagnostic,
            )

        if kind == "request_clarification":
            if not clarification_question:
                return IntentArbitrationResult(
                    kind="unclassified",
                    diagnostic="classifier_missing_clarification",
                )
            return IntentArbitrationResult(
                kind="request_clarification",
                clarification_question=clarification_question,
                diagnostic=diagnostic,
            )

        return IntentArbitrationResult(kind="unclassified", diagnostic=diagnostic)

    def _extract_json_object(self, content: str | None) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

        match = self._JSON_BLOCK_RE.search(text)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None
