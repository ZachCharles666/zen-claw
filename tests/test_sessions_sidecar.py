import asyncio
import json as jsonlib

import httpx

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.sessions import (
    SessionsKillTool,
    SessionsListTool,
    SessionsReadTool,
    SessionsResizeTool,
    SessionsSignalTool,
    SessionsSpawnTool,
    SessionsWriteTool,
)
from zen_claw.bus.queue import MessageBus
from zen_claw.config.schema import ExecToolConfig
from zen_claw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        get_response: _FakeResponse | None = None,
        post_response: _FakeResponse | None = None,
        exc: Exception | None = None,
    ):
        self._get_response = get_response or _FakeResponse(200, {"ok": True})
        self._post_response = post_response or _FakeResponse(200, {"ok": True})
        self._exc = exc

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(
        self, url: str, headers: dict | None = None, params: dict | None = None
    ) -> _FakeResponse:
        if self._exc:
            raise self._exc
        assert url
        return self._get_response

    async def post(
        self,
        url: str,
        headers: dict | None = None,
        json: dict | None = None,
        content: bytes | None = None,
    ) -> _FakeResponse:
        if self._exc:
            raise self._exc
        assert url
        if content is not None:
            assert len(content) > 0
        return self._post_response


async def test_sessions_spawn_sidecar_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(200, {"ok": True, "session_id": "s-1", "status": "running"})
        ),
    )
    tool = SessionsSpawnTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(command="echo hi")
    assert result.ok is True
    assert result.meta.get("session_id") == "s-1"


async def test_sessions_spawn_includes_pty_payload(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class _CaptureClient(_FakeClient):
        async def post(
            self,
            url: str,
            headers: dict | None = None,
            json: dict | None = None,
            content: bytes | None = None,
        ) -> _FakeResponse:
            observed["json"] = json or {}
            return _FakeResponse(200, {"ok": True, "session_id": "s-pty", "status": "running"})

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _CaptureClient(),
    )
    tool = SessionsSpawnTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(command="python -i", pty=True)
    assert result.ok is True
    assert isinstance(observed.get("json"), dict)
    assert observed["json"].get("pty") is True


async def test_sessions_list_sidecar_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            get_response=_FakeResponse(
                200,
                {
                    "ok": True,
                    "sessions": [
                        {"session_id": "s-1", "status": "running", "command": "echo hi"},
                    ],
                },
            )
        ),
    )
    tool = SessionsListTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute()
    assert result.ok is True
    assert "s-1" in result.content


async def test_sessions_kill_not_found_returns_parameter_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(
                404,
                {"ok": False, "error_code": "session_not_found", "error_message": "missing"},
            )
        ),
    )
    tool = SessionsKillTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(session_id="s-missing")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PARAMETER
    assert result.error.code == "session_not_found"


async def test_sessions_read_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            get_response=_FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "chunk": "hello\n",
                    "next_cursor": 6,
                    "truncated": False,
                },
            )
        ),
    )
    tool = SessionsReadTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(session_id="s-1", cursor=0, max_bytes=128)
    assert result.ok is True
    assert result.meta.get("next_cursor") == 6
    assert result.meta.get("chunk") == "hello\n"


async def test_sessions_write_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "written_bytes": 5,
                },
            )
        ),
    )
    tool = SessionsWriteTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(session_id="s-1", input="hello")
    assert result.ok is True
    assert result.meta.get("written_bytes") == 5


async def test_sessions_signal_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "signal": "interrupt",
                    "delivered": True,
                },
            )
        ),
    )
    tool = SessionsSignalTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(session_id="s-1", signal="interrupt")
    assert result.ok is True
    assert result.meta.get("delivered") is True


async def test_sessions_resize_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "rows": 40,
                    "cols": 120,
                    "applied": True,
                },
            )
        ),
    )
    tool = SessionsResizeTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(session_id="s-1", rows=40, cols=120)
    assert result.ok is True
    assert result.meta.get("applied") is True


async def test_sessions_tools_healthcheck_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(get_response=_FakeResponse(503, {"ok": False})),
    )
    tool = SessionsListTool(
        sidecar_exec_url="http://127.0.0.1:4488/v1/exec",
        sidecar_healthcheck=True,
    )
    result = await tool.execute()
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "sessions_sidecar_unhealthy"


async def test_sessions_tools_unreachable_returns_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(exc=httpx.RequestError("down")),
    )
    tool = SessionsSpawnTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(command="echo hi")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.RETRYABLE
    assert result.error.code == "sessions_unreachable"


async def test_sessions_tools_pass_trace_id_header(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def get(
            self, url: str, headers: dict | None = None, params: dict | None = None
        ) -> _FakeResponse:
            observed["trace"] = (headers or {}).get("X-Trace-Id", "")
            return _FakeResponse(200, {"ok": True, "sessions": []})

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )
    tool = SessionsListTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(trace_id="trace-abc")
    assert result.ok is True
    assert observed.get("trace") == "trace-abc"


async def test_sessions_spawn_permission_error_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _FakeClient(
            post_response=_FakeResponse(
                403,
                {"ok": False, "error_code": "approval_required", "error_message": "no token"},
            )
        ),
    )
    tool = SessionsSpawnTool(sidecar_exec_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute(command="echo hi")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "approval_required"


async def test_sessions_list_hmac_adds_signature_headers(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def get(
            self, url: str, headers: dict | None = None, params: dict | None = None
        ) -> _FakeResponse:
            h = headers or {}
            observed["sig"] = h.get("X-Approval-Signature", "")
            observed["ts"] = h.get("X-Approval-Timestamp", "")
            observed["trace"] = h.get("X-Trace-Id", "")
            assert "X-Approval-Token" not in h
            return _FakeResponse(200, {"ok": True, "sessions": []})

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )
    tool = SessionsListTool(
        sidecar_exec_url="http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode="hmac",
        sidecar_approval_token="secret",
    )
    result = await tool.execute(trace_id="trace-abc")
    assert result.ok is True
    assert observed.get("trace") == "trace-abc"
    assert observed.get("sig")
    assert observed.get("ts")


async def test_sessions_read_hmac_adds_signature_headers(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def get(
            self, url: str, headers: dict | None = None, params: dict | None = None
        ) -> _FakeResponse:
            h = headers or {}
            observed["sig"] = h.get("X-Approval-Signature", "")
            observed["ts"] = h.get("X-Approval-Timestamp", "")
            observed["trace"] = h.get("X-Trace-Id", "")
            observed["url"] = url
            assert "X-Approval-Token" not in h
            return _FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "chunk": "",
                    "next_cursor": 0,
                },
            )

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )
    tool = SessionsReadTool(
        sidecar_exec_url="http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode="hmac",
        sidecar_approval_token="secret",
    )
    result = await tool.execute(session_id="s-1", trace_id="trace-abc")
    assert result.ok is True
    assert observed.get("trace") == "trace-abc"
    assert observed.get("sig")
    assert observed.get("ts")
    assert observed.get("url", "").endswith("/v1/sessions/s-1/read")


async def test_sessions_write_hmac_adds_signature_headers(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def post(
            self,
            url: str,
            headers: dict | None = None,
            json: dict | None = None,
            content: bytes | None = None,
        ) -> _FakeResponse:
            h = headers or {}
            observed["sig"] = h.get("X-Approval-Signature", "")
            observed["ts"] = h.get("X-Approval-Timestamp", "")
            observed["trace"] = h.get("X-Trace-Id", "")
            observed["url"] = url
            assert "X-Approval-Token" not in h
            return _FakeResponse(
                200, {"ok": True, "session_id": "s-1", "status": "running", "written_bytes": 6}
            )

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )
    tool = SessionsWriteTool(
        sidecar_exec_url="http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode="hmac",
        sidecar_approval_token="secret",
    )
    result = await tool.execute(session_id="s-1", input="status", trace_id="trace-abc")
    assert result.ok is True
    assert observed.get("trace") == "trace-abc"
    assert observed.get("sig")
    assert observed.get("ts")
    assert observed.get("url", "").endswith("/v1/sessions/s-1/write")


async def test_sessions_signal_hmac_adds_signature_headers(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def post(
            self,
            url: str,
            headers: dict | None = None,
            json: dict | None = None,
            content: bytes | None = None,
        ) -> _FakeResponse:
            h = headers or {}
            observed["sig"] = h.get("X-Approval-Signature", "")
            observed["ts"] = h.get("X-Approval-Timestamp", "")
            observed["trace"] = h.get("X-Trace-Id", "")
            observed["url"] = url
            assert "X-Approval-Token" not in h
            return _FakeResponse(
                200,
                {
                    "ok": True,
                    "session_id": "s-1",
                    "status": "running",
                    "signal": "interrupt",
                    "delivered": True,
                },
            )

    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )
    tool = SessionsSignalTool(
        sidecar_exec_url="http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode="hmac",
        sidecar_approval_token="secret",
    )
    result = await tool.execute(session_id="s-1", signal="interrupt", trace_id="trace-abc")
    assert result.ok is True
    assert observed.get("trace") == "trace-abc"
    assert observed.get("sig")
    assert observed.get("ts")
    assert observed.get("url", "").endswith("/v1/sessions/s-1/signal")


class _DummyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


class _QueueProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(api_key=None, api_base=None)
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "fake-model"


def test_agent_loop_sidecar_sessions_spawn_write_read_workflow(tmp_path, monkeypatch) -> None:
    class _NoopSessionManager:
        def __init__(self, workspace):
            self.workspace = workspace

        def get_or_create(self, key):
            class _S:
                metadata = {}
                messages = []

                def add_message(self, role, content, **kwargs):
                    self.messages.append({"role": role, "content": content, **kwargs})

                def get_history(self, max_messages=50):
                    return []

            return _S()

        def save(self, session):
            return None

    requests_seen: list[tuple[str, str, dict]] = []

    class _WorkflowClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None, content=None):
            payload = json if isinstance(json, dict) else {}
            if not payload and content:
                payload = dict(jsonlib.loads(content.decode("utf-8")))
            requests_seen.append(("POST", url, payload))
            if url.endswith("/v1/sessions/start"):
                return _FakeResponse(200, {"ok": True, "session_id": "s-42", "status": "running"})
            if url.endswith("/v1/sessions/s-42/write"):
                return _FakeResponse(
                    200, {"ok": True, "session_id": "s-42", "status": "running", "written_bytes": 6}
                )
            return _FakeResponse(
                404, {"ok": False, "error_code": "route_not_found", "error_message": "missing"}
            )

        async def get(self, url, headers=None, params=None):
            requests_seen.append(("GET", url, params or {}))
            if url.endswith("/v1/sessions/s-42/read"):
                return _FakeResponse(
                    200,
                    {
                        "ok": True,
                        "session_id": "s-42",
                        "status": "running",
                        "chunk": "ready\n",
                        "next_cursor": 6,
                        "truncated": False,
                    },
                )
            return _FakeResponse(
                404, {"ok": False, "error_code": "session_not_found", "error_message": "missing"}
            )

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _NoopSessionManager)
    monkeypatch.setattr(
        "zen_claw.agent.tools.sessions.httpx.AsyncClient", lambda timeout: _WorkflowClient()
    )
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.get_always_skills", lambda self: [])
    monkeypatch.setattr("zen_claw.agent.skills.SkillsLoader.build_skills_summary", lambda self: "")
    monkeypatch.setattr(
        "zen_claw.agent.skills.SkillsLoader.load_skills_for_context", lambda self, names: ""
    )

    provider = _QueueProvider(
        [
            LLMResponse(
                content="start session",
                tool_calls=[
                    ToolCallRequest(
                        id="t1", name="sessions_spawn", arguments={"command": "cat", "pty": True}
                    )
                ],
            ),
            LLMResponse(
                content="write input",
                tool_calls=[
                    ToolCallRequest(
                        id="t2",
                        name="sessions_write",
                        arguments={"session_id": "s-42", "input": "status"},
                    )
                ],
            ),
            LLMResponse(
                content="read output",
                tool_calls=[
                    ToolCallRequest(
                        id="t3", name="sessions_read", arguments={"session_id": "s-42", "cursor": 0}
                    )
                ],
            ),
            LLMResponse(content="workflow done"),
        ]
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        exec_config=ExecToolConfig(mode="sidecar"),
        enable_planning=False,
        max_reflections=1,
        max_iterations=6,
    )
    loop.memory_extractor.should_extract = lambda user_text, assistant_text: False  # type: ignore[assignment]

    out = asyncio.run(
        loop.process_direct("run interactive workflow", channel="cli", chat_id="direct")
    )
    assert out == "workflow done"
    assert provider.calls == 4
    assert len(requests_seen) == 3
    assert requests_seen[0][0] == "POST" and requests_seen[0][1].endswith("/v1/sessions/start")
    assert requests_seen[1][0] == "POST" and requests_seen[1][1].endswith("/v1/sessions/s-42/write")
    assert requests_seen[2][0] == "GET" and requests_seen[2][1].endswith("/v1/sessions/s-42/read")


def test_agent_loop_registers_sessions_tools_only_in_sidecar_mode(tmp_path, monkeypatch) -> None:
    class _NoopSessionManager:
        def __init__(self, workspace):
            self.workspace = workspace

    monkeypatch.setattr("zen_claw.agent.loop.SessionManager", _NoopSessionManager)

    sidecar_cfg = ExecToolConfig(mode="sidecar")
    loop_sidecar = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        exec_config=sidecar_cfg,
    )
    assert loop_sidecar.tools.has("sessions_spawn")
    assert loop_sidecar.tools.has("sessions_list")
    assert loop_sidecar.tools.has("sessions_kill")
    assert loop_sidecar.tools.has("sessions_read")
    assert loop_sidecar.tools.has("sessions_write")
    assert loop_sidecar.tools.has("sessions_signal")
    assert loop_sidecar.tools.has("sessions_resize")

    local_cfg = ExecToolConfig(mode="local")
    loop_local = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
        exec_config=local_cfg,
    )
    assert not loop_local.tools.has("sessions_spawn")
