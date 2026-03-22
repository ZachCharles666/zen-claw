"""Tests for ContentGenTool and ComplianceCheckTool agent tool wrappers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(content: str) -> MagicMock:
    """Return a mock LLMProvider whose chat() returns *content*."""
    response = MagicMock()
    response.content = content
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=response)
    return provider


def _ok_json(tool_result: Any) -> dict:
    """Assert ToolResult is ok and parse JSON content."""
    assert tool_result.ok, f"Expected ok=True, got error: {getattr(tool_result, 'error', None)}"
    return json.loads(tool_result.content)


# ---------------------------------------------------------------------------
# ContentGenTool
# ---------------------------------------------------------------------------


class TestContentGenTool:
    @pytest.mark.asyncio
    async def test_template_fallback_no_provider(self, tmp_path: Path) -> None:
        """ContentGenTool should succeed in template mode when no provider is given."""
        from zen_claw.agent.tools.business_skills import ContentGenTool

        tool = ContentGenTool(data_dir=tmp_path, provider=None)
        result = await tool.execute(
            industry="finance",
            channel="wechat_moments",
            product_name="稳健理财A",
            audience="中老年投资者",
            count=1,
        )
        payload = _ok_json(result)
        assert payload["channel"] == "wechat_moments"
        assert payload["product_name"] == "稳健理财A"
        assert len(payload["versions"]) == 1
        assert payload["versions"][0]["source"] == "template"

    @pytest.mark.asyncio
    async def test_llm_path_with_provider(self, tmp_path: Path) -> None:
        """ContentGenTool should use LLM path when a provider is configured."""
        from zen_claw.agent.tools.business_skills import ContentGenTool

        llm_payload = json.dumps(
            {
                "headline": "理财新选择",
                "body": "专为稳健投资者设计",
                "cta": "立即了解详情",
                "review_notes": [],
                "grounding_used": [],
            }
        )
        provider = _make_provider(llm_payload)
        # Patch semantic review to avoid a second LLM call
        with patch(
            "zen_claw.skills.compliance_check.checker.ComplianceChecker._semantic_review",
            new_callable=AsyncMock,
            return_value={"status": "not_available", "risk_level": "low", "rewrite_needed": False,
                          "confidence": "rule_only", "concerns": [], "recommended_actions": []},
        ):
            tool = ContentGenTool(data_dir=tmp_path, provider=provider)
            result = await tool.execute(
                industry="finance",
                channel="generic",
                product_name="产品X",
                audience="普通投资者",
                count=1,
            )
        payload = _ok_json(result)
        assert payload["versions"][0]["source"] == "llm"
        assert payload["versions"][0]["structured"]["headline"] == "理财新选择"

    @pytest.mark.asyncio
    async def test_missing_required_param_returns_failure(self, tmp_path: Path) -> None:
        """ContentGenTool should return PARAMETER error when product_name is empty."""
        from zen_claw.agent.tools.business_skills import ContentGenTool
        from zen_claw.agent.tools.result import ToolErrorKind

        tool = ContentGenTool(data_dir=tmp_path, provider=None)
        result = await tool.execute(
            industry="finance",
            channel="generic",
            product_name="",
            audience="投资者",
            count=1,
        )
        assert not result.ok
        assert result.error is not None
        assert result.error.kind == ToolErrorKind.PARAMETER

    @pytest.mark.asyncio
    async def test_xiaohongshu_channel(self, tmp_path: Path) -> None:
        """ContentGenTool should include hashtags for xiaohongshu channel."""
        from zen_claw.agent.tools.business_skills import ContentGenTool

        tool = ContentGenTool(data_dir=tmp_path, provider=None)
        result = await tool.execute(
            industry="consumer",
            channel="xiaohongshu",
            product_name="护肤精华",
            audience="18-30岁女性",
            count=1,
        )
        payload = _ok_json(result)
        structured = payload["versions"][0]["structured"]
        assert "hashtags" in structured
        assert "hook" in structured

    @pytest.mark.asyncio
    async def test_douyin_channel(self, tmp_path: Path) -> None:
        """ContentGenTool should include beats for douyin_script channel."""
        from zen_claw.agent.tools.business_skills import ContentGenTool

        tool = ContentGenTool(data_dir=tmp_path, provider=None)
        result = await tool.execute(
            industry="consumer",
            channel="douyin_script",
            product_name="新品饮料",
            audience="年轻消费者",
            count=1,
        )
        payload = _ok_json(result)
        structured = payload["versions"][0]["structured"]
        assert "beats" in structured
        assert isinstance(structured["beats"], list)

    @pytest.mark.asyncio
    async def test_compliance_check_included_in_versions(self, tmp_path: Path) -> None:
        """Each generated version should include a compliance result."""
        from zen_claw.agent.tools.business_skills import ContentGenTool

        tool = ContentGenTool(data_dir=tmp_path, provider=None)
        result = await tool.execute(
            industry="finance",
            channel="generic",
            product_name="理财产品",
            audience="投资者",
            count=2,
        )
        payload = _ok_json(result)
        assert len(payload["versions"]) == 2
        for version in payload["versions"]:
            assert "compliance" in version
            assert "score" in version["compliance"]
            assert "risk_level" in version["compliance"]


# ---------------------------------------------------------------------------
# ComplianceCheckTool
# ---------------------------------------------------------------------------


class TestComplianceCheckTool:
    @pytest.mark.asyncio
    async def test_rules_only_flags_high_risk(self) -> None:
        """ComplianceCheckTool should flag finance-sensitive terms as high risk."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(
            text="保证收益8%，稳赚不赔，零风险产品。",
            industry="finance",
        )
        payload = _ok_json(result)
        assert payload["risk_level"] in {"high", "medium"}
        assert len(payload["issues"]) > 0

    @pytest.mark.asyncio
    async def test_rules_only_clean_text_low_risk(self) -> None:
        """ComplianceCheckTool should return low risk for clean neutral text."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(
            text="欢迎了解我们面向稳健型投资者的理财方案，具体收益请参阅产品说明书。",
            industry="finance",
        )
        payload = _ok_json(result)
        assert payload["score"] > 60

    @pytest.mark.asyncio
    async def test_semantic_false_no_semantic_review_key(self) -> None:
        """Without semantic=True the result should not include semantic_review."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(text="普通产品介绍文案。")
        payload = _ok_json(result)
        assert "semantic_review" not in payload

    @pytest.mark.asyncio
    async def test_semantic_true_no_provider_returns_not_available(self) -> None:
        """semantic=True without provider should return not_available status in semantic_review."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        # provider=None → semantic_review.status == "not_available"
        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(
            text="保证高收益产品。",
            semantic=True,
        )
        payload = _ok_json(result)
        assert "semantic_review" in payload
        assert payload["semantic_review"]["status"] == "not_available"

    @pytest.mark.asyncio
    async def test_semantic_true_with_provider(self) -> None:
        """semantic=True with a provider should call LLM and return ok status."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        llm_response = json.dumps(
            {
                "risk_level": "high",
                "rewrite_needed": True,
                "confidence": "high",
                "concerns": ["绝对化承诺收益违反广告法"],
                "recommended_actions": ["删除收益承诺表述"],
                "rationale": "文案包含明确的收益保证，属高风险表述。",
            }
        )
        provider = _make_provider(llm_response)
        tool = ComplianceCheckTool(provider=provider)
        result = await tool.execute(
            text="保证年化10%收益，绝对安全。",
            industry="finance",
            semantic=True,
        )
        payload = _ok_json(result)
        assert "semantic_review" in payload
        assert payload["semantic_review"]["status"] == "ok"
        assert payload["semantic_review"]["reviewer"] == "llm_semantic"
        assert payload["semantic_review"]["risk_level"] == "high"

    @pytest.mark.asyncio
    async def test_channel_specific_rule_douyin(self) -> None:
        """ComplianceCheckTool should flag strong-push terms on douyin channel."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(
            text="限时特价，立刻下单！",
            channel="douyin",
        )
        payload = _ok_json(result)
        # should have at least one channel-specific issue
        rules = [issue["rule"] for issue in payload["issues"]]
        assert any("强刺激" in r or "营销" in r for r in rules)

    @pytest.mark.asyncio
    async def test_ad_law_absolute_term_flagged(self) -> None:
        """ComplianceCheckTool should flag absolute superlatives."""
        from zen_claw.agent.tools.business_skills import ComplianceCheckTool

        tool = ComplianceCheckTool(provider=None)
        result = await tool.execute(text="这是市场上最好的理财产品，第一安全。")
        payload = _ok_json(result)
        rules = [issue["rule"] for issue in payload["issues"]]
        assert any("广告法" in r for r in rules)
