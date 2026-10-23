"""Tests for the OpenAI-compatible /v1/chat/completions adapter."""

from __future__ import annotations

import json

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from zen_claw.dashboard.server import api_app


@pytest.fixture
def client():
    if TestClient is None or api_app is None:
        pytest.skip("fastapi is not available")
    return TestClient(api_app)


@pytest.fixture(autouse=True)
def _mock_invoke(monkeypatch):
    """Patch _invoke_agent_text so tests don't require a live LLM provider."""

    async def _fake(message: str, session_id: str) -> str:
        return f"[echo] {message}"

    monkeypatch.setattr(
        "zen_claw.api.openai_compat._invoke",
        _fake,
    )


# ── /v1/models ──────────────────────────────────────────────────────────────


class TestListModels:
    def test_list_models_returns_zen_claw(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) >= 1
        ids = [m["id"] for m in body["data"]]
        assert "zen-claw" in ids

    def test_list_models_schema(self, client):
        resp = client.get("/v1/models")
        model = resp.json()["data"][0]
        assert model["object"] == "model"
        assert "created" in model
        assert "owned_by" in model


# ── /v1/chat/completions (non-streaming) ────────────────────────────────────


class TestChatCompletions:
    def test_basic_completion(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "zen-claw"
        assert len(body["choices"]) == 1

        choice = body["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["role"] == "assistant"
        assert "[echo] hello" in choice["message"]["content"]

    def test_completion_id_format(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "test"}],
            },
        )
        body = resp.json()
        assert body["id"].startswith("chatcmpl-")
        assert isinstance(body["created"], int)

    def test_usage_tokens_present(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "count tokens"}],
            },
        )
        usage = resp.json()["usage"]
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "total_tokens" in usage
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    def test_empty_message_returns_400(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "  "}],
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["type"] == "invalid_request_error"

    def test_no_user_message_returns_400(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "system", "content": "you are helpful"}],
            },
        )
        assert resp.status_code == 400

    def test_multi_turn_extracts_last_user(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "second"},
                ],
            },
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "second" in content

    def test_system_message_prepended_to_user_content(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [
                    {"role": "system", "content": "Answer with 最终答案:"},
                    {"role": "user", "content": "What is 2+2?"},
                ],
            },
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        # System message should be included in the content passed to agent
        assert "最终答案:" in content
        assert "What is 2+2?" in content

    def test_custom_model_name_echoed(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "my-custom-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.json()["model"] == "my-custom-model"

    def test_extra_params_ignored(self, client):
        """SDK may send params we don't use; they should not crash the endpoint."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.5,
                "top_p": 0.9,
                "n": 1,
                "presence_penalty": 0.0,
                "frequency_penalty": 0.0,
                "stop": ["\n"],
            },
        )
        assert resp.status_code == 200

    def test_xbench_reply_is_normalized_to_short_final_answer(self, client, monkeypatch):
        async def _fake(_message: str, _session_id: str) -> str:
            return "根据搜索结果，2012年为71，2020年为72。\n最终答案：8年间增加了1.1岁。"

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "你是一个通用人工智能助手。对于本题, 你的答案必须是尽可能简洁的数值。\n"
                            "最终答案:[你的答案]\n\n[问题]: 示例问题"
                        ),
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "最终答案: 1.1岁"

    def test_xbench_reply_preserves_long_title_final_answer(self, client, monkeypatch):
        title = (
            "ProlificDreamer: High-Fidelity and Diverse Text-to-3D Generation "
            "with Variational Score Distillation"
        )

        async def _fake(_message: str, _session_id: str) -> str:
            return f"最终答案: {title}"

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "user": "xbench",
                "messages": [{"role": "user", "content": "benchmark prompt"}],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == f"最终答案: {title}"

    def test_xbench_reply_preserves_chinese_book_title_marks(self, client, monkeypatch):
        async def _fake(_message: str, _session_id: str) -> str:
            return "最终答案：《示例书名》"

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "user": "xbench",
                "messages": [{"role": "user", "content": "benchmark prompt"}],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "最终答案: 《示例书名》"

    def test_xbench_prompt_gets_eval_instruction_and_session_marker(self, client, monkeypatch):
        captured = {}

        async def _fake(message: str, session_id: str) -> str:
            captured["message"] = message
            captured["session_id"] = session_id
            return "最终答案：42"

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "你是一个通用人工智能助手。对于本题, 你的答案必须是短语。\n"
                            "最终答案:[你的答案]\n\n[问题]: 示例问题"
                        ),
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert "xbench评测硬性要求" in captured["message"]
        assert captured["session_id"].startswith("xbench:")

    def test_xbench_user_marker_uses_isolated_session_per_request(self, client, monkeypatch):
        captured_sessions = []

        async def _fake(_message: str, session_id: str) -> str:
            captured_sessions.append(session_id)
            return "最终答案：42"

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        payload = {
            "model": "zen-claw",
            "user": "xbench",
            "messages": [{"role": "user", "content": "benchmark prompt"}],
        }

        first = client.post("/v1/chat/completions", json=payload)
        second = client.post("/v1/chat/completions", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(captured_sessions) == 2
        assert captured_sessions[0].startswith("xbench:xbench:chatcmpl-")
        assert captured_sessions[1].startswith("xbench:xbench:chatcmpl-")
        assert captured_sessions[0] != captured_sessions[1]

    def test_xbench_prompt_detection_does_not_require_generic_assistant_text(
        self, client, monkeypatch
    ):
        captured = {}

        async def _fake(message: str, session_id: str) -> str:
            captured["message"] = message
            captured["session_id"] = session_id
            return "I've completed processing but have no response to give."

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [
                    {
                        "role": "user",
                        "content": "最终答案:[你的答案]\n\n[问题]: 示例问题",
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert "xbench评测硬性要求" in captured["message"]
        assert captured["session_id"].startswith("xbench:")
        assert resp.json()["choices"][0]["message"]["content"] == "最终答案: 无"

    def test_xbench_user_marker_forces_eval_mode(self, client, monkeypatch):
        captured = {}

        async def _fake(message: str, session_id: str) -> str:
            captured["message"] = message
            captured["session_id"] = session_id
            return "I've completed processing but have no response to give."

        monkeypatch.setattr("zen_claw.api.openai_compat._invoke", _fake)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "user": "xbench",
                "messages": [{"role": "user", "content": "plain benchmark prompt"}],
            },
        )

        assert resp.status_code == 200
        assert "xbench评测硬性要求" in captured["message"]
        assert captured["session_id"].startswith("xbench:")
        assert resp.json()["choices"][0]["message"]["content"] == "最终答案: 无"


# ── /v1/chat/completions (streaming) ────────────────────────────────────────


class TestChatCompletionsStream:
    def test_stream_returns_sse(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_stream_ends_with_done(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        text = resp.text
        assert "data: [DONE]" in text

    def test_stream_chunks_are_valid_json(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        chunks = []
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                assert payload["object"] == "chat.completion.chunk"
                chunks.append(payload)
        assert len(chunks) > 0

    def test_stream_first_chunk_has_role(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                first = json.loads(line[6:])
                assert first["choices"][0]["delta"].get("role") == "assistant"
                break

    def test_stream_last_data_chunk_has_finish_reason(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        data_lines = [
            line.strip()
            for line in resp.text.split("\n")
            if line.strip().startswith("data: ") and line.strip() != "data: [DONE]"
        ]
        last = json.loads(data_lines[-1][6:])
        assert last["choices"][0]["finish_reason"] == "stop"

    def test_stream_reassembles_full_reply(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "zen-claw",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        content_parts = []
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[6:])
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    content_parts.append(delta["content"])
        full = "".join(content_parts)
        assert "[echo] hello" in full
