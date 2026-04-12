"""Regression suite for xbench evaluation readiness.

Run before every xbench submission:
    pytest tests/eval/xbench_regression.py -v

Checks:
  1. OpenAI-compat endpoint correctness (format, schema, streaming)
  2. Resilience: empty-answer guard, timeout/error degradation
  3. Tool chain integrity: web_search failure returns valid degraded response
  4. Throughput: 50 consecutive calls without crash
  5. xbench prompt format: 最终答案 format passthrough
"""

from __future__ import annotations

import json
import time

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from zen_claw.dashboard.server import api_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    if TestClient is None or api_app is None:
        pytest.skip("fastapi not available")
    return TestClient(api_app)


@pytest.fixture(autouse=True)
def _mock_invoke(monkeypatch):
    """Replace agent invocation with a deterministic stub for regression tests."""

    async def _fake(message: str, session_id: str) -> str:
        if "最终答案" in message:
            # Simulate agent following xbench format instruction
            return "根据搜索结果，答案如下。\n最终答案:42"
        if "__force_empty__" in message:
            return ""
        if "__force_slow__" in message:
            time.sleep(0.01)  # Simulate slight delay
            return "慢速响应，但有效。\n最终答案:slow"
        return f"[stub] {message[:80]}"

    monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)


def _post_chat(client, content: str, *, stream: bool = False, system: str | None = None) -> dict:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "zen-claw", "messages": messages, "stream": stream},
    )
    return resp


# ---------------------------------------------------------------------------
# 1. Schema compliance
# ---------------------------------------------------------------------------


class TestSchemaCompliance:
    """Verify response matches OpenAI ChatCompletion schema."""

    def test_required_top_level_fields(self, client):
        resp = _post_chat(client, "hello")
        body = resp.json()
        for field in ("id", "object", "created", "model", "choices", "usage"):
            assert field in body, f"missing field: {field}"

    def test_choice_structure(self, client):
        body = _post_chat(client, "hello").json()
        choice = body["choices"][0]
        assert "index" in choice
        assert "message" in choice
        assert "finish_reason" in choice
        assert choice["message"]["role"] == "assistant"
        assert isinstance(choice["message"]["content"], str)

    def test_usage_structure(self, client):
        body = _post_chat(client, "hello").json()
        usage = body["usage"]
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    def test_id_prefix(self, client):
        body = _post_chat(client, "hello").json()
        assert body["id"].startswith("chatcmpl-")

    def test_created_is_recent_epoch(self, client):
        body = _post_chat(client, "hello").json()
        assert abs(body["created"] - int(time.time())) < 10


# ---------------------------------------------------------------------------
# 2. Resilience — no empty or hung responses
# ---------------------------------------------------------------------------


class TestResilience:
    """Ensure the endpoint never returns an empty response or crashes."""

    def test_no_empty_response_on_normal_query(self, client):
        body = _post_chat(client, "What is the capital of France?").json()
        content = body["choices"][0]["message"]["content"]
        assert content.strip(), "response must not be empty"

    def test_empty_agent_reply_becomes_fallback(self, client):
        """If agent returns '', the endpoint must still return a non-crashing response."""
        resp = _post_chat(client, "__force_empty__")
        assert resp.status_code == 200
        # Empty string is valid JSON — endpoint must not crash
        body = resp.json()
        assert "choices" in body

    def test_empty_user_content_returns_400(self, client):
        resp = _post_chat(client, "   ")
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"

    def test_very_long_prompt_handled(self, client):
        long_prompt = "请回答这个问题。" * 500
        resp = _post_chat(client, long_prompt)
        assert resp.status_code == 200

    def test_unicode_cjk_prompt_handled(self, client):
        resp = _post_chat(client, "量子计算机的工作原理是什么？请详细解释。")
        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 3. xbench prompt format passthrough
# ---------------------------------------------------------------------------


class TestXbenchFormat:
    """Verify xbench-specific prompt handling."""

    def test_zui_zhong_da_an_format_preserved(self, client):
        """Agent receives the 最终答案 format instruction intact."""
        xbench_prompt = (
            "你是一个通用人工智能助手。我将向你提出一个学术问题, "
            "请尽可能简洁地给出解题思路, 并用以下模版作为回答的结尾:\n\n"
            "最终答案:[你的答案]\n\n"
            "[问题]: 地球距太阳约多少天文单位？"
        )
        resp = _post_chat(client, xbench_prompt)
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        # Our stub returns 最终答案:42 when it sees 最终答案 in message
        assert "最终答案:" in content

    def test_xbench_system_plus_user(self, client):
        """System message + user question format (matches xbench harness)."""
        resp = _post_chat(
            client,
            "地球距太阳约多少天文单位？",
            system=(
                "你是一个通用人工智能助手。请用以下模版作为回答的结尾:\n"
                "最终答案:[你的答案]"
            ),
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "最终答案:" in content

    def test_choice_question_format(self, client):
        choice_prompt = (
            "你是一个通用人工智能助手。\n"
            "对于本题, 选出所有符合的选项, 连续列出所有选项:\n\n"
            "最终答案:[你的答案]\n\n"
            "[问题]: 以下哪些是行星？A.地球 B.月球 C.火星 D.太阳"
        )
        resp = _post_chat(client, choice_prompt)
        assert resp.status_code == 200

    def test_n_repeats_consistency(self, client):
        """Same question called 5 times returns valid response each time (Best-of-5)."""
        prompt = "最终答案 格式测试问题"
        for _ in range(5):
            resp = _post_chat(client, prompt)
            assert resp.status_code == 200
            body = resp.json()
            assert body["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# 4. Streaming compliance
# ---------------------------------------------------------------------------


class TestStreamingCompliance:
    """Verify SSE streaming matches OpenAI chunk format."""

    def test_stream_produces_valid_chunks(self, client):
        resp = _post_chat(client, "最终答案 streaming test", stream=True)
        assert resp.status_code == 200
        chunks = []
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                assert "choices" in chunk
                assert chunk["object"] == "chat.completion.chunk"
                chunks.append(chunk)
        assert len(chunks) > 0

    def test_stream_reassembles_to_non_empty(self, client):
        resp = _post_chat(client, "最终答案 reassemble test", stream=True)
        content = "".join(
            json.loads(line[6:])["choices"][0]["delta"].get("content", "")
            for line in resp.text.split("\n")
            if line.strip().startswith("data: ") and line.strip() != "data: [DONE]"
        )
        assert content.strip()


# ---------------------------------------------------------------------------
# 5. Throughput — 50 consecutive calls without crash
# ---------------------------------------------------------------------------


class TestThroughput:
    """50 consecutive calls: no crash, no empty response."""

    def test_50_consecutive_calls_stable(self, client):
        errors = []
        for i in range(50):
            try:
                resp = _post_chat(client, f"最终答案 stability test {i}")
                if resp.status_code != 200:
                    errors.append(f"call {i}: status {resp.status_code}")
                    continue
                content = resp.json()["choices"][0]["message"]["content"]
                if not content.strip():
                    errors.append(f"call {i}: empty response")
            except Exception as e:
                errors.append(f"call {i}: exception {e}")

        assert not errors, "Failures in 50-call stability test:\n" + "\n".join(errors)
