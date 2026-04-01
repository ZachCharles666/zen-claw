import pytest

from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.skills.compliance_check.checker import ComplianceChecker
from zen_claw.skills.content_gen.generator import ContentGenerator


class _FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (tools, model, max_tokens, temperature)
        self.calls += 1
        user_msg = str(messages[-1]["content"])
        if "Copy under review:" in user_msg:
            return LLMResponse(
                content=(
                    '{"risk_level":"medium","rewrite_needed":true,"confidence":"high",'
                    '"concerns":["claim needs softer framing"],'
                    '"recommended_actions":["add caveats"],'
                    '"rationale":"semantic layer flagged stronger-than-neutral positioning"}'
                )
            )
        if "Return JSON only." in user_msg:
            return LLMResponse(
                content=(
                    '{"headline":"LLM headline","body":"LLM body","cta":"LLM CTA",'
                    '"review_notes":["keep claim grounded"],'
                    '"grounding_used":["kb.md"],'
                    '"rationale":"draft contract"}'
                )
            )
        return LLMResponse(content="LLM generated draft")

    def get_default_model(self) -> str:
        return "fake-model"


class _PlaintextProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (messages, tools, model, max_tokens, temperature)
        return LLMResponse(content="Plaintext draft only")

    def get_default_model(self) -> str:
        return "plain-model"


class _FailingProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (messages, tools, model, max_tokens, temperature)
        raise RuntimeError("provider unavailable")

    def get_default_model(self) -> str:
        return "failing-model"


@pytest.mark.asyncio
async def test_generator_uses_template_fallback() -> None:
    generator = ContentGenerator()
    result = await generator.generate(
        industry="finance",
        channel="wechat_moments",
        product_name="稳健组合",
        audience="理财用户",
        key_points=["突出稳健配置", "提醒先看风险"],
        count=2,
    )
    assert len(result["versions"]) == 2
    assert result["contract_version"] == "alpha.v1"
    assert result["request_contract"]["required"] == [
        "industry",
        "channel",
        "product_name",
        "audience",
    ]
    assert "hook" in result["output_schema"]
    assert result["versions"][0]["source"] == "template"
    assert result["versions"][0]["execution"]["provider_enabled"] is False
    assert result["versions"][0]["structured"]["hook"]
    assert result["versions"][0]["structured"]["hashtags"]


@pytest.mark.asyncio
async def test_generator_uses_provider_when_available() -> None:
    generator = ContentGenerator(provider=_FakeProvider())
    result = await generator.generate(
        industry="finance",
        channel="generic",
        product_name="稳健组合",
        audience="理财用户",
        count=1,
    )
    assert result["versions"][0]["source"] == "llm"
    assert result["execution"]["provider_enabled"] is True
    assert result["versions"][0]["content"] == "LLM headline\nLLM body\nLLM CTA"
    assert result["versions"][0]["execution"]["provider_status"] == "ok"
    assert result["versions"][0]["structured"]["headline"] == "LLM headline"
    assert result["versions"][0]["structured"]["review_notes"] == ["keep claim grounded"]
    assert result["versions"][0]["structured"]["grounding_used"] == ["kb.md"]
    assert result["versions"][0]["compliance"]["semantic_review"]["status"] == "ok"
    assert result["versions"][0]["compliance"]["semantic_review"]["risk_level"] == "medium"


@pytest.mark.asyncio
async def test_generator_falls_back_when_provider_does_not_return_json() -> None:
    generator = ContentGenerator(provider=_PlaintextProvider())
    result = await generator.generate(
        industry="general",
        channel="generic",
        product_name="会员服务",
        audience="新用户",
        count=1,
    )
    assert result["versions"][0]["source"] == "llm_plaintext"
    assert result["versions"][0]["execution"]["provider_status"] == "plaintext"
    assert result["versions"][0]["content"] == "Plaintext draft only"
    assert result["versions"][0]["structured"]["headline"] == "Plaintext draft only"


@pytest.mark.asyncio
async def test_generator_falls_back_to_template_when_provider_raises() -> None:
    generator = ContentGenerator(
        provider=_FailingProvider(),
        compliance_checker=ComplianceChecker(),
    )
    result = await generator.generate(
        industry="general",
        channel="generic",
        product_name="会员服务",
        audience="新用户",
        count=1,
    )

    assert result["execution"]["fallback_used"] is True
    assert result["versions"][0]["source"] == "template_fallback"
    assert result["versions"][0]["execution"]["provider_status"] == "fallback_template"
    assert result["versions"][0]["execution"]["provider_error"] == "provider unavailable"
    assert "会员服务" in result["versions"][0]["content"]
