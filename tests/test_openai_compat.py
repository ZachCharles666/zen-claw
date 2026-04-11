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
