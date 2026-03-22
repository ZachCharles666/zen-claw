import pytest

from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.skills.compliance_check.checker import ComplianceChecker


def test_checker_flags_finance_and_ad_risk() -> None:
    checker = ComplianceChecker()
    result = checker.check_text("本产品保证收益，属于行业第一。", industry="finance")
    assert result["risk_level"] == "high"
    assert result["score"] < 100
    assert any(issue["severity"] == "high" for issue in result["issues"])
    assert any("人工确认" in note for note in result["review_notes"])
class _BrokenSemanticProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (messages, tools, model, max_tokens, temperature)
        return LLMResponse(content="not json")

    def get_default_model(self) -> str:
        return "broken-semantic-model"


@pytest.mark.asyncio
async def test_checker_semantic_review_falls_back_when_provider_response_is_invalid() -> None:
    checker = ComplianceChecker(provider=_BrokenSemanticProvider())
    result = await checker.review_text("本产品保证收益，属于行业第一。", industry="finance")

    assert result["semantic_review"]["status"] == "invalid_response"
    assert result["semantic_review"]["reviewer"] == "llm_semantic"
    assert result["semantic_review"]["risk_level"] == "high"
    assert result["semantic_review"]["rewrite_needed"] is True
