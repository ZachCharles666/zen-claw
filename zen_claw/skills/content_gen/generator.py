"""Business-facing content generation with deterministic fallback and optional LLM path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.providers.base import LLMProvider
from zen_claw.skills.compliance_check.checker import ComplianceChecker
from zen_claw.skills.rag_retrieve.retriever import RAGRetrieverSkill


class ContentGenerator:
    """Generate channel-specific draft copy with optional grounding and compliance checks."""

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        compliance_checker: ComplianceChecker | None = None,
        rag_retriever: RAGRetrieverSkill | None = None,
        templates_dir: Path | None = None,
    ):
        self._provider = provider
        self._compliance = compliance_checker or ComplianceChecker(provider=provider)
        self._rag = rag_retriever
        self._templates_dir = templates_dir or (Path(__file__).parent / "templates")

    async def generate(
        self,
        *,
        industry: str,
        channel: str,
        product_name: str,
        audience: str,
        style: str = "",
        key_points: list[str] | None = None,
        count: int = 3,
        use_rag: bool = False,
        notebook_id: str = "",
        rag_filters: dict[str, Any] | None = None,
        rag_source_contains: str = "",
        rag_tenant_id: str = "",
        rag_store_backend: str = "",
    ) -> dict[str, Any]:
        channel = channel.strip() or "generic"
        product_name = product_name.strip()
        audience = audience.strip()
        industry = str(industry or "").strip()
        if not product_name:
            raise ValueError("product_name is required")
        if not audience:
            raise ValueError("audience is required")

        key_points = [point.strip() for point in (key_points or []) if point.strip()]
        count = max(1, min(5, int(count)))
        references: list[dict[str, Any]] = []
        if use_rag and self._rag is not None:
            rag_payload = self._rag.retrieve(
                product_name,
                notebook_id=notebook_id,
                top_k=min(3, count + 1),
                source_contains=rag_source_contains,
                filters=rag_filters,
                tenant_id=rag_tenant_id,
                store_backend=rag_store_backend,
            )
            references = list(rag_payload.get("results") or [])

        strategy = self._build_strategy(
            industry=industry,
            channel=channel,
            audience=audience,
            style=style,
            references=references,
        )
        template = self._load_template(channel)
        versions = []
        provider_enabled = self._provider is not None
        for idx in range(count):
            provider_status = "disabled"
            provider_error = ""
            if self._provider is None:
                content = self._render_fallback(
                    template=template,
                    product_name=product_name,
                    audience=audience,
                    style=style,
                    key_points=key_points,
                    references=references,
                    variant_index=idx,
                )
                source = "template"
                llm_structured: dict[str, Any] = {}
            else:
                prompt = self._build_prompt(
                    template=template,
                    industry=industry,
                    channel=channel,
                    product_name=product_name,
                    audience=audience,
                    style=style,
                    key_points=key_points,
                    references=references,
                    variant_index=idx,
                )
                try:
                    response = await self._provider.chat(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "You write grounded marketing drafts and avoid unsupported claims. "
                                    "Return strict JSON with keys: headline, body, cta, review_notes, "
                                    "grounding_used, and optional channel-specific keys."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=min(1.0, 0.7 + (idx * 0.1)),
                    )
                    llm_structured = self._parse_provider_payload(response.content)
                    content = self._provider_content_fallback(response.content, llm_structured)
                    provider_status = "ok" if llm_structured else "plaintext"
                    source = "llm" if llm_structured else "llm_plaintext"
                except Exception as exc:
                    provider_status = "fallback_template"
                    provider_error = str(exc)
                    llm_structured = {}
                    content = self._render_fallback(
                        template=template,
                        product_name=product_name,
                        audience=audience,
                        style=style,
                        key_points=key_points,
                        references=references,
                        variant_index=idx,
                    )
                    source = "template_fallback"
            structured = self._shape_channel_output(
                channel=channel,
                product_name=product_name,
                audience=audience,
                content=content,
                key_points=key_points,
                references=references,
                llm_structured=llm_structured,
            )
            compliance_result = await self._compliance.review_text(
                content,
                industry=industry,
                channel=channel,
            )
            versions.append(
                {
                    "variant": idx + 1,
                    "content": content,
                    "structured": structured,
                    "source": source,
                    "execution": {
                        "provider_enabled": provider_enabled,
                        "provider_status": provider_status,
                        "provider_error": provider_error,
                        "reference_count": len(references),
                    },
                    "compliance": compliance_result,
                }
            )

        return {
            "contract_version": "alpha.v1",
            "channel": channel,
            "product_name": product_name,
            "request_contract": {
                "required": ["industry", "channel", "product_name", "audience"],
                "optional": [
                    "style",
                    "key_points",
                    "count",
                    "use_rag",
                    "notebook_id",
                    "rag_filters",
                    "rag_source_contains",
                    "rag_tenant_id",
                    "rag_store_backend",
                ],
            },
            "strategy": strategy,
            "output_schema": self._channel_schema(channel),
            "execution": {
                "provider_enabled": provider_enabled,
                "rag_requested": bool(use_rag),
                "rag_used": bool(references),
                "reference_count": len(references),
                "fallback_used": any(
                    str(item.get("source") or "").startswith("template") for item in versions
                ),
            },
            "references": references,
            "versions": versions,
        }

    def _load_template(self, channel: str) -> str:
        candidates = [
            self._templates_dir / f"{channel}.md",
            self._templates_dir / "generic.md",
        ]
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError("content generation template not found")

    @staticmethod
    def _build_strategy(
        *,
        industry: str,
        channel: str,
        audience: str,
        style: str,
        references: list[dict[str, Any]],
    ) -> str:
        parts = [
            f"channel={channel}",
            f"audience={audience}",
            f"industry={industry or 'general'}",
            f"style={style or 'clear and trustworthy'}",
        ]
        if references:
            parts.append(f"references={len(references)} grounded snippets")
        return "; ".join(parts)

    @staticmethod
    def _render_fallback(
        *,
        template: str,
        product_name: str,
        audience: str,
        style: str,
        key_points: list[str],
        references: list[dict[str, Any]],
        variant_index: int,
    ) -> str:
        opener_bank = [
            f"给{audience}的一条直白提醒：",
            f"如果你正在为{audience}准备内容，可以先抓住这个重点：",
            f"围绕{audience}沟通时，建议先把价值说清楚：",
        ]
        opener = opener_bank[variant_index % len(opener_bank)]
        bullet_points = key_points or ["突出核心价值", "避免夸大承诺", "保留行动引导"]
        reference_line = ""
        if references:
            cited = references[0]
            reference_line = f"\n参考资料：{cited['source']} - {cited['content'][:60]}"
        return template.format(
            opener=opener,
            product_name=product_name,
            audience=audience,
            style=style or "清晰可信",
            key_points="；".join(bullet_points),
            reference_line=reference_line,
        ).strip()

    @staticmethod
    def _build_prompt(
        *,
        template: str,
        industry: str,
        channel: str,
        product_name: str,
        audience: str,
        style: str,
        key_points: list[str],
        references: list[dict[str, Any]],
        variant_index: int,
    ) -> str:
        reference_block = "\n".join(
            f"- {item['source']}: {item['content']}" for item in references[:3]
        )
        return (
            f"Channel: {channel}\n"
            f"Industry: {industry or 'general'}\n"
            f"Audience: {audience}\n"
            f"Product: {product_name}\n"
            f"Style: {style or 'clear and trustworthy'}\n"
            f"Variant: {variant_index + 1}\n"
            f"Key points: {', '.join(key_points) if key_points else 'Highlight value and keep claims grounded.'}\n"
            f"Grounding references:\n{reference_block or '- none'}\n\n"
            "Return JSON only. Required fields: headline, body, cta, review_notes, grounding_used. "
            "Optional channel fields should follow the channel contract.\n\n"
            f"Template:\n{template}"
        )

    @staticmethod
    def _channel_schema(channel: str) -> dict[str, Any]:
        normalized = str(channel or "").strip().lower()
        shared = {
            "headline": "short title or opening line",
            "body": "main draft body",
            "cta": "call to action",
            "evidence_notes": "grounding/source notes when present",
            "review_notes": ["operator/compliance review notes"],
            "grounding_used": ["reference sources used in the draft"],
        }
        if normalized == "wechat_article":
            return {
                **shared,
                "summary": "article summary line",
                "sections": ["problem", "solution", "action"],
            }
        if normalized == "wechat_moments":
            return {
                **shared,
                "hook": "short first-line hook",
                "hashtags": ["optional hashtag list"],
            }
        if normalized == "xiaohongshu":
            return {
                **shared,
                "hook": "lifestyle opening hook",
                "bullets": ["experience-oriented bullets"],
                "hashtags": ["discovery hashtags"],
            }
        if normalized == "douyin_script":
            return {
                **shared,
                "hook": "opening hook",
                "beats": ["hook", "core point", "action close"],
            }
        return shared

    @staticmethod
    def _shape_channel_output(
        *,
        channel: str,
        product_name: str,
        audience: str,
        content: str,
        key_points: list[str],
        references: list[dict[str, Any]],
        llm_structured: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = str(channel or "").strip().lower()
        clean_lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        headline = clean_lines[0] if clean_lines else product_name
        body = "\n".join(clean_lines[1:] if len(clean_lines) > 1 else clean_lines)
        cta = f"欢迎联系获取更适合{audience}的方案。"
        evidence_notes = ", ".join(
            str(item.get("source") or "").strip()
            for item in references
            if str(item.get("source") or "").strip()
        )
        llm_structured = llm_structured or {}
        payload: dict[str, Any] = {
            "headline": str(llm_structured.get("headline") or headline).strip() or product_name,
            "body": str(llm_structured.get("body") or body).strip() or content.strip(),
            "cta": str(llm_structured.get("cta") or cta).strip() or cta,
            "evidence_notes": evidence_notes,
            "review_notes": ContentGenerator._normalize_string_list(
                llm_structured.get("review_notes")
            ),
            "grounding_used": ContentGenerator._normalize_string_list(
                llm_structured.get("grounding_used")
            )
            or [item.get("source", "") for item in references if str(item.get("source") or "").strip()],
        }
        if normalized == "wechat_article":
            payload["summary"] = str(
                llm_structured.get("summary") or f"{product_name}面向{audience}的图文摘要"
            ).strip()
            sections = llm_structured.get("sections")
            payload["sections"] = {
                "problem": ContentGenerator._section_value(
                    sections, "problem", fallback=payload["headline"]
                ),
                "solution": ContentGenerator._section_value(
                    sections, "solution", fallback=payload["body"] or f"围绕{product_name}解释核心价值。"
                ),
                "action": ContentGenerator._section_value(sections, "action", fallback=payload["cta"]),
            }
        elif normalized == "wechat_moments":
            payload["hook"] = str(llm_structured.get("hook") or payload["headline"]).strip()
            payload["hashtags"] = ContentGenerator._normalize_string_list(
                llm_structured.get("hashtags")
            ) or [f"#{product_name}", f"#{audience}"]
        elif normalized == "xiaohongshu":
            payload["hook"] = str(llm_structured.get("hook") or payload["headline"]).strip()
            payload["bullets"] = ContentGenerator._normalize_string_list(
                llm_structured.get("bullets")
            ) or key_points or [f"{product_name}适合{audience}"]
            payload["hashtags"] = ContentGenerator._normalize_string_list(
                llm_structured.get("hashtags")
            ) or [f"#{product_name}", "#经验分享"]
        elif normalized == "douyin_script":
            payload["hook"] = str(llm_structured.get("hook") or payload["headline"]).strip()
            payload["beats"] = ContentGenerator._normalize_string_list(
                llm_structured.get("beats")
            ) or [
                payload["headline"],
                payload["body"]
                or f"核心信息：{'；'.join(key_points) if key_points else product_name}",
                payload["cta"],
            ]
        return payload

    @staticmethod
    def _provider_content_fallback(raw: str | None, structured: dict[str, Any]) -> str:
        headline = str(structured.get("headline") or "").strip()
        body = str(structured.get("body") or "").strip()
        cta = str(structured.get("cta") or "").strip()
        if headline or body or cta:
            return "\n".join(part for part in [headline, body, cta] if part).strip()
        return str(raw or "").strip()

    @staticmethod
    def _parse_provider_payload(raw: str | None) -> dict[str, Any]:
        body = str(raw or "").strip()
        if not body:
            return {}
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{")
            end = body.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                data = json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _section_value(sections: Any, key: str, *, fallback: str) -> str:
        if isinstance(sections, dict):
            value = str(sections.get(key) or "").strip()
            if value:
                return value
        return fallback
