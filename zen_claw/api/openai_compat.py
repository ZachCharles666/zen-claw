"""OpenAI-compatible ``/v1/chat/completions`` adapter for zen-claw.

This module exposes a FastAPI router that accepts requests in the
`OpenAI Chat Completion <https://platform.openai.com/docs/api-reference/chat>`_
format and forwards them through the zen-claw agent loop, returning
responses in the same format so that the ``openai`` Python SDK (and
evaluation harnesses like **xbench-evals**) can interact with zen-claw
transparently.

Supported features
------------------
* ``POST /v1/chat/completions`` (non-streaming)
* ``POST /v1/chat/completions`` with ``stream: true`` (SSE)
* ``GET  /v1/models`` (model listing)

Usage::

    from zen_claw.api.openai_compat import openai_router
    app.include_router(openai_router)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

openai_router = APIRouter(tags=["openai-compat"])

# ---------------------------------------------------------------------------
# Pydantic request / response models (subset of the OpenAI spec)
# ---------------------------------------------------------------------------


class _ChatMessage(BaseModel):
    role: str = "user"
    content: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """Subset of OpenAI ``CreateChatCompletionRequest``."""

    model: str = "zen-claw"
    messages: list[_ChatMessage]
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = None
    stream: bool = False
    # Accepted but ignored — keeps SDK callers from crashing.
    top_p: float | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    user: str | None = None


class _ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None


class _Choice(BaseModel):
    index: int = 0
    message: _ChoiceMessage
    finish_reason: str = "stop"


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """Subset of OpenAI ``CreateChatCompletionResponse``."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[_Choice]
    usage: _Usage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_user_content(messages: list[_ChatMessage]) -> str:
    """Return the last user message content, falling back to empty string."""
    for msg in reversed(messages):
        if msg.role == "user" and msg.content:
            return msg.content
    return ""


def _make_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _now_epoch() -> int:
    return int(time.time())


def _estimate_tokens(text: str) -> int:
    """Rough token count (1 token ~ 4 chars for English, ~1.5 for CJK)."""
    return max(1, len(text) // 3)


async def _invoke(message: str, session_id: str) -> str:
    """Invoke the agent loop — thin wrapper around the dashboard helper."""
    # Import lazily so the module can be loaded even when config is absent.
    from zen_claw.dashboard.server import _invoke_agent_text

    return await _invoke_agent_text(message, session_id)


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


@openai_router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    """OpenAI-compatible chat completion endpoint."""
    user_content = _extract_user_content(body.messages)
    if not user_content.strip():
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "No user message content found in messages.",
                    "type": "invalid_request_error",
                    "code": "invalid_messages",
                }
            },
        )

    # Use a stable session key derived from the ``user`` field or a random id.
    session_id = body.user or uuid.uuid4().hex[:16]
    completion_id = _make_completion_id()
    created = _now_epoch()

    if body.stream:
        return _stream_response(
            user_content, session_id, completion_id, created, body.model
        )

    # ── Non-streaming path ────────────────────────────────────────────────
    reply = await _invoke(user_content, session_id)

    prompt_tokens = sum(_estimate_tokens(m.content or "") for m in body.messages)
    completion_tokens = _estimate_tokens(reply)

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[
            _Choice(
                message=_ChoiceMessage(content=reply),
                finish_reason="stop",
            )
        ],
        usage=_Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# Streaming (SSE)
# ---------------------------------------------------------------------------


def _stream_response(
    user_content: str,
    session_id: str,
    completion_id: str,
    created: int,
    model: str,
) -> StreamingResponse:
    """Return a ``StreamingResponse`` that emits OpenAI-style SSE chunks."""

    async def _generate():
        reply = await _invoke(user_content, session_id)
        # Emit tokens character-by-character (matches existing dashboard SSE
        # behaviour).  A future optimisation may integrate with true streaming
        # from the LLM provider.
        for i, ch in enumerate(reply):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": ch} if i > 0 else {"role": "assistant", "content": ch},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        # Final chunk with finish_reason
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@openai_router.get("/v1/models")
async def list_models():
    """Return a minimal model list so ``openai.models.list()`` works."""
    now = _now_epoch()
    return {
        "object": "list",
        "data": [
            {
                "id": "zen-claw",
                "object": "model",
                "created": now,
                "owned_by": "zen-claw",
            }
        ],
    }
