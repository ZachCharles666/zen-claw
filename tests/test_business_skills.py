from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zen_claw.auth.paths import tenant_data_dir
from zen_claw.knowledge.notebook import NotebookManager
from zen_claw.knowledge.retriever import HybridSearchResult
from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.skills.compliance_check.checker import ComplianceChecker
from zen_claw.skills.rag_retrieve.retriever import RAGRetrieverSkill


class _SemanticProvider(LLMProvider):
    def __init__(self, payload: str):
        super().__init__()
        self.payload = payload

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (messages, tools, model, max_tokens, temperature)
        return LLMResponse(content=self.payload)

    def get_default_model(self) -> str:
        return "semantic-model"


def test_compliance_checker_returns_structured_risk_report() -> None:
    checker = ComplianceChecker()
    result = checker.check_text(
        "本产品保证收益，收益率12%，属于行业第一。",
        industry="finance",
        channel="wechat_moments",
    )
    assert result["risk_level"] == "high"
    assert result["score"] <= 63
    assert len(result["issues"]) >= 3
    assert any(issue["rule"] == "禁止承诺收益" for issue in result["issues"])


def test_compliance_checker_returns_low_risk_for_neutral_copy() -> None:
    checker = ComplianceChecker()
    result = checker.check_text("欢迎查看本周活动详情，具体规则以页面说明为准。")
    assert result["risk_level"] == "low"
    assert result["score"] == 100
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_compliance_checker_layers_semantic_review_when_provider_exists() -> None:
    checker = ComplianceChecker(
        provider=_SemanticProvider(
            (
                '{"risk_level":"medium","rewrite_needed":true,"confidence":"high",'
                '"concerns":["收益表述仍然偏强"],'
                '"recommended_actions":["补充适用条件和风险提示"],'
                '"rationale":"虽然没有直接承诺，但收益表达仍需收敛。"}'
            )
        )
    )
    result = await checker.review_text(
        "本周策略力求提升组合收益表现。",
        industry="finance",
        channel="wechat_article",
    )

    assert result["semantic_review"]["status"] == "ok"
    assert result["semantic_review"]["reviewer"] == "llm_semantic"
    assert result["semantic_review"]["risk_level"] == "medium"
    assert result["semantic_review"]["rewrite_needed"] is True
    assert result["semantic_review"]["concerns"] == ["收益表述仍然偏强"]


@pytest.mark.asyncio
async def test_compliance_checker_returns_rule_only_semantic_stub_without_provider() -> None:
    checker = ComplianceChecker()
    result = await checker.review_text("欢迎查看本周活动详情，具体规则以页面说明为准。")

    assert result["semantic_review"]["status"] == "not_available"
    assert result["semantic_review"]["reviewer"] == "deterministic_only"
    assert result["semantic_review"]["risk_level"] == "low"


def test_rag_retriever_skill_raises_for_missing_notebook(tmp_path: Path) -> None:
    skill = RAGRetrieverSkill(tmp_path)
    with pytest.raises(ValueError, match="notebook not found"):
        skill.retrieve("hello")


def test_rag_retriever_skill_filters_by_source(tmp_path: Path) -> None:
    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("kb")
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook") as mock_factory:
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [
            HybridSearchResult(
                content="Finance policy",
                source="finance_policy.md",
                score=0.8,
                page=2,
                rrf_score=0.9,
            ),
            HybridSearchResult(
                content="Retail FAQ",
                source="retail_faq.md",
                score=0.7,
                page=None,
                rrf_score=0.7,
            ),
        ]
        mock_factory.return_value = mock_retriever
        skill = RAGRetrieverSkill(tmp_path, default_notebook="kb")
        result = skill.retrieve("policy", source_contains="finance")

    assert result["notebook"] == "kb"
    assert result["tenant_id"] == "default"
    assert result["source_contains"] == "finance"
    assert len(result["results"]) == 1
    assert result["results"][0]["source"] == "finance_policy.md"


def test_rag_retriever_skill_supports_tenant_and_metadata_filters(tmp_path: Path) -> None:
    tenant_id = "11111111-1111-1111-1111-111111111111"
    manager = NotebookManager(tenant_data_dir(tmp_path, tenant_id))
    manager.create("kb")
    with patch("zen_claw.knowledge.retriever.HybridRetriever.from_notebook") as mock_factory:
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [
            HybridSearchResult(
                content="Finance policy",
                source="finance_policy.md",
                score=0.8,
                page=2,
                rrf_score=0.9,
            )
        ]
        mock_factory.return_value = mock_retriever
        skill = RAGRetrieverSkill(tmp_path, default_notebook="kb")
        result = skill.retrieve(
            "policy",
            tenant_id=tenant_id,
            store_backend="memory",
            filters={"industry": "finance", "tier": 1, "ignored": ["x"]},
        )

    assert result["tenant_id"] == tenant_id
    assert result["store_backend"] == "memory"
    assert result["filters"] == {"industry": "finance", "tier": 1}
