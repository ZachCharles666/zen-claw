"""Tool wrappers for business skills: content generation and compliance review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zen_claw.agent.tools.base import Tool
from zen_claw.agent.tools.result import ToolErrorKind, ToolResult
from zen_claw.auth.paths import DEFAULT_TENANT_ID
from zen_claw.providers.base import LLMProvider


class ContentGenTool(Tool):
    """Generate channel-specific marketing copy with optional RAG grounding and compliance check."""

    name = "content_gen"
    description = (
        "Generate channel-specific marketing draft copy. "
        "Supported channels: wechat_moments, wechat_article, xiaohongshu, douyin_script, generic. "
        "Optionally grounds output with local knowledge retrieval and runs compliance precheck "
        "on every generated version."
    )
    parameters = {
        "type": "object",
        "properties": {
            "industry": {
                "type": "string",
                "description": (
                    "Industry context (e.g. finance, real_estate, consumer). "
                    "Shapes compliance rules and writing guidance."
                ),
            },
            "channel": {
                "type": "string",
                "enum": [
                    "generic",
                    "wechat_moments",
                    "wechat_article",
                    "xiaohongshu",
                    "douyin_script",
                ],
                "description": "Target delivery channel. Determines output structure and tone.",
            },
            "product_name": {
                "type": "string",
                "description": "Product or service being promoted.",
            },
            "audience": {
                "type": "string",
                "description": "Target audience description.",
            },
            "style": {
                "type": "string",
                "description": "Tone and writing style guidance.",
            },
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key selling points or facts to include.",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Number of variants to generate. Defaults to 3.",
            },
            "use_rag": {
                "type": "boolean",
                "description": "Ground output with local knowledge retrieval.",
            },
            "notebook_id": {
                "type": "string",
                "description": "Knowledge notebook ID used when use_rag is true.",
            },
        },
        "required": ["industry", "channel", "product_name", "audience"],
    }

    def __init__(
        self,
        data_dir: Path,
        *,
        provider: LLMProvider | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._provider = provider
        self._tenant_id = tenant_id

    async def execute(  # type: ignore[override]
        self,
        industry: str,
        channel: str,
        product_name: str,
        audience: str,
        style: str = "",
        key_points: list[str] | None = None,
        count: int = 3,
        use_rag: bool = False,
        notebook_id: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        from zen_claw.skills.compliance_check.checker import ComplianceChecker
        from zen_claw.skills.content_gen.generator import ContentGenerator
        from zen_claw.skills.rag_retrieve.retriever import RAGRetrieverSkill

        try:
            rag: RAGRetrieverSkill | None = None
            if use_rag:
                rag = RAGRetrieverSkill(
                    self._data_dir,
                    tenant_id=self._tenant_id,
                )
            checker = ComplianceChecker(provider=self._provider)
            generator = ContentGenerator(
                provider=self._provider,
                compliance_checker=checker,
                rag_retriever=rag,
            )
            result = await generator.generate(
                industry=industry,
                channel=channel,
                product_name=product_name,
                audience=audience,
                style=style,
                key_points=key_points,
                count=count,
                use_rag=use_rag,
                notebook_id=notebook_id,
                rag_tenant_id=self._tenant_id,
            )
            return ToolResult.success(json.dumps(result, ensure_ascii=False, default=str))
        except ValueError as exc:
            return ToolResult.failure(
                ToolErrorKind.PARAMETER,
                str(exc),
                code="content_gen_invalid_param",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"content generation failed: {exc}",
                code="content_gen_error",
            )


class ComplianceCheckTool(Tool):
    """Review marketing copy for compliance issues using rules engine and optional LLM semantic review."""

    name = "compliance_check"
    description = (
        "Review marketing copy for compliance risk. "
        "Returns risk level (low/medium/high), specific issues with positions and rewrite suggestions. "
        "Set semantic=true to layer an LLM semantic review on top of the rules engine."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Marketing copy text to review.",
            },
            "industry": {
                "type": "string",
                "description": (
                    "Industry context (e.g. finance, banking, insurance) "
                    "for domain-specific compliance rules."
                ),
            },
            "channel": {
                "type": "string",
                "description": "Delivery channel for channel-specific rule tuning.",
            },
            "policy_context": {
                "type": "string",
                "description": "Additional compliance policy context for the LLM semantic review.",
            },
            "semantic": {
                "type": "boolean",
                "description": (
                    "Include LLM semantic review on top of rules engine. "
                    "Requires a configured LLM provider. Defaults to false."
                ),
            },
        },
        "required": ["text"],
    }

    def __init__(self, *, provider: LLMProvider | None = None) -> None:
        self._provider = provider

    async def execute(  # type: ignore[override]
        self,
        text: str,
        industry: str = "",
        channel: str = "",
        policy_context: str = "",
        semantic: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        from zen_claw.skills.compliance_check.checker import ComplianceChecker

        try:
            checker = ComplianceChecker(
                provider=self._provider if semantic else None,
            )
            if semantic:
                result = await checker.review_text(
                    text,
                    industry=industry,
                    channel=channel,
                    policy_context=policy_context,
                )
            else:
                result = checker.check_text(text, industry=industry, channel=channel)
            return ToolResult.success(json.dumps(result, ensure_ascii=False, default=str))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                ToolErrorKind.RUNTIME,
                f"compliance check failed: {exc}",
                code="compliance_check_error",
            )
