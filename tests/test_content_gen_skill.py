import pytest

from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.skills.content_gen.generator import ContentGenerator


class _FakeProvider(LLMProvider):
    def __init__(self):
        super().__init__()

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        _ = (tools, model, max_tokens, temperature)
        user_msg = str(messages[-1]["content"])
        if "Copy under review:" in user_msg:
            return LLMResponse(
                content=(
                    '{"risk_level":"low","rewrite_needed":false,"confidence":"medium",'
                    '"concerns":["keep channel claim grounded"],'
                    '"recommended_actions":["retain neutral wording"],'
                    '"rationale":"draft stays descriptive and does not overclaim"}'
                )
            )
        if "Return JSON only." in user_msg:
            return LLMResponse(
                content=(
                    '{"headline":"Provider headline","body":"Provider body","cta":"Provider CTA",'
                    '"review_notes":["double-check channel fit"],'
                    '"grounding_used":["kb.md"],'
                    '"hook":"Provider hook","hashtags":["#会员服务","#新用户"]}'
                )
            )
        return LLMResponse(content="Provider draft for channel")

    def get_default_model(self) -> str:
        return "fake-model"


class _FakeRAG:
    def retrieve(
        self,
        query: str,
        *,
        notebook_id: str = "",
        top_k: int = 5,
        source_contains: str = "",
        filters=None,
        tenant_id: str = "",
        store_backend: str = "",
    ):
        _ = (notebook_id, top_k, source_contains, filters, tenant_id, store_backend)
        return {
            "query": query,
            "results": [
                {
                    "content": "Grounded product facts",
                    "source": "kb.md",
                    "score": 0.9,
                    "page": 1,
                }
            ],
        }


@pytest.mark.asyncio
async def test_content_generator_fallback_uses_templates_and_compliance() -> None:
    generator = ContentGenerator(rag_retriever=_FakeRAG())
    result = await generator.generate(
        industry="finance",
        channel="wechat_article",
        product_name="安心理财方案",
        audience="稳健型用户",
        style="专业但温和",
        key_points=["解释适用人群", "避免收益承诺"],
        count=2,
        use_rag=True,
        rag_filters={"industry": "finance"},
        rag_source_contains="kb",
    )
    assert result["strategy"].startswith("channel=wechat_article")
    assert "sections" in result["output_schema"]
    assert result["references"][0]["source"] == "kb.md"
    assert len(result["versions"]) == 2
    assert result["versions"][0]["source"] == "template"
    assert "安心理财方案" in result["versions"][0]["content"]
    assert result["versions"][0]["structured"]["sections"]["action"]
    assert "compliance" in result["versions"][0]
    assert result["versions"][0]["compliance"]["semantic_review"]["status"] == "not_available"


@pytest.mark.asyncio
async def test_content_generator_provider_path_returns_llm_content() -> None:
    generator = ContentGenerator(provider=_FakeProvider())
    result = await generator.generate(
        industry="general",
        channel="generic",
        product_name="会员服务",
        audience="新用户",
        count=1,
    )
    assert result["versions"][0]["source"] == "llm"
    assert result["versions"][0]["content"] == "Provider headline\nProvider body\nProvider CTA"
    assert result["versions"][0]["structured"]["headline"] == "Provider headline"
    assert result["versions"][0]["structured"]["review_notes"] == ["double-check channel fit"]
    assert result["versions"][0]["compliance"]["semantic_review"]["status"] == "ok"


@pytest.mark.asyncio
async def test_content_generator_requires_product_and_audience() -> None:
    generator = ContentGenerator()
    with pytest.raises(ValueError, match="product_name is required"):
        await generator.generate(
            industry="general",
            channel="generic",
            product_name="",
            audience="用户",
        )
    with pytest.raises(ValueError, match="audience is required"):
        await generator.generate(
            industry="general",
            channel="generic",
            product_name="产品",
            audience="",
        )
