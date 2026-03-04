import httpx

from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.shell import ExecTool


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        exc: Exception | None = None,
        health_response: _FakeResponse | None = None,
    ):
        self._response = response
        self._exc = exc
        self._health_response = health_response or _FakeResponse(200, {"ok": True})

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, headers: dict, json: dict | None = None, content: bytes | None = None) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert url
        if json is not None:
            assert "command" in json
        if content is not None:
            assert len(content) > 0
        return self._response or _FakeResponse(500, {"ok": False})

    async def get(self, url: str) -> _FakeResponse:
        assert url
        return self._health_response


async def test_exec_tool_sidecar_success(monkeypatch) -> None:
    response = _FakeResponse(200, {"ok": True, "stdout": "hello", "exit_code": 0})
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(response=response),
    )

    tool = ExecTool(mode="sidecar", sidecar_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute("echo hello")
    assert result.ok is True
    assert "hello" in result.content


async def test_exec_tool_sidecar_permission_error(monkeypatch) -> None:
    response = _FakeResponse(
        403,
        {"ok": False, "error_code": "approval_required", "error_message": "token missing"},
    )
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(response=response),
    )

    tool = ExecTool(mode="sidecar", sidecar_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute("echo hello")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "approval_required"


async def test_exec_tool_sidecar_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(exc=httpx.TimeoutException("timed out")),
    )

    tool = ExecTool(mode="sidecar", sidecar_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute("echo hello")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.RETRYABLE
    assert result.error.code == "exec_sidecar_timeout"


async def test_exec_tool_sidecar_fallbacks_to_local(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(exc=httpx.RequestError("down")),
    )

    async def fake_local(command: str, cwd: str, env=None):
        from zen_claw.agent.tools.result import ToolResult

        return ToolResult.success("local-ok", fallback="local")

    tool = ExecTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4488/v1/exec",
        sidecar_fallback_to_local=True,
    )
    monkeypatch.setattr(tool, "_execute_local", fake_local)
    result = await tool.execute("echo hello")
    assert result.ok is True
    assert result.content == "local-ok"


async def test_exec_tool_sidecar_healthcheck_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(health_response=_FakeResponse(503, {"ok": False})),
    )
    tool = ExecTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4488/v1/exec",
        sidecar_healthcheck=True,
    )
    result = await tool.execute("echo hello")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "exec_sidecar_unhealthy"


async def test_exec_tool_sidecar_healthcheck_failed_then_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _FakeClient(health_response=_FakeResponse(503, {"ok": False})),
    )

    async def fake_local(command: str, cwd: str, env=None):
        from zen_claw.agent.tools.result import ToolResult

        return ToolResult.success("local-from-healthcheck")

    tool = ExecTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4488/v1/exec",
        sidecar_healthcheck=True,
        sidecar_fallback_to_local=True,
    )
    monkeypatch.setattr(tool, "_execute_local", fake_local)
    result = await tool.execute("echo hello")
    assert result.ok is True
    assert result.content == "local-from-healthcheck"


async def test_exec_tool_sidecar_passes_trace_id_header(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def post(self, url: str, headers: dict, json: dict) -> _FakeResponse:
            observed["trace"] = headers.get("X-Trace-Id", "")
            return _FakeResponse(200, {"ok": True, "stdout": "ok", "exit_code": 0})

    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )

    tool = ExecTool(mode="sidecar", sidecar_url="http://127.0.0.1:4488/v1/exec")
    result = await tool.execute("echo hello", trace_id="trace-xyz")
    assert result.ok is True
    assert observed.get("trace") == "trace-xyz"


async def test_exec_tool_sidecar_hmac_adds_signature_headers(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class _HeaderClient(_FakeClient):
        async def post(
            self,
            url: str,
            headers: dict,
            json: dict | None = None,
            content: bytes | None = None,
        ) -> _FakeResponse:
            observed["sig"] = headers.get("X-Approval-Signature", "")
            observed["ts"] = headers.get("X-Approval-Timestamp", "")
            observed["trace"] = headers.get("X-Trace-Id", "")
            assert "X-Approval-Token" not in headers
            assert content is not None and len(content) > 0
            return _FakeResponse(200, {"ok": True, "stdout": "ok", "exit_code": 0})

    monkeypatch.setattr(
        "zen_claw.agent.tools.shell.httpx.AsyncClient",
        lambda timeout: _HeaderClient(),
    )

    tool = ExecTool(
        mode="sidecar",
        sidecar_url="http://127.0.0.1:4488/v1/exec",
        sidecar_approval_mode="hmac",
        sidecar_approval_token="secret",
    )
    result = await tool.execute("echo hello", trace_id="trace-xyz")
    assert result.ok is True
    assert observed.get("trace") == "trace-xyz"
    assert observed.get("sig")
    assert observed.get("ts")


# ── tests: MEDIUM-004 — URL-encoded path traversal bypass ────────────────────


def _guarded_tool(cwd: str = "/workspace") -> ExecTool:
    return ExecTool(restrict_to_workspace=True)


def test_guard_blocks_plain_traversal() -> None:
    tool = _guarded_tool()
    assert tool._guard_command("cat ../../etc/passwd", "/workspace") is not None


def test_guard_blocks_url_encoded_traversal() -> None:
    """Single URL-encoded: %2e%2e/ must be caught after unquote (MEDIUM-004)."""
    tool = _guarded_tool()
    assert tool._guard_command("cat %2e%2e%2fetc%2fpasswd", "/workspace") is not None


def test_guard_blocks_double_encoded_traversal() -> None:
    """Double URL-encoded: %252e%252e/ must be caught after double unquote (MEDIUM-004)."""
    tool = _guarded_tool()
    assert tool._guard_command("cat %252e%252e%252fetc%252fpasswd", "/workspace") is not None


def test_guard_blocks_null_byte_injection() -> None:
    """Null byte in command must be caught (MEDIUM-004)."""
    tool = _guarded_tool()
    assert tool._guard_command("cat /workspace/file\x00../../etc/passwd", "/workspace") is not None


def test_guard_allows_safe_command() -> None:
    """A clean command inside workspace must not be blocked."""
    tool = _guarded_tool()
    assert tool._guard_command("ls /workspace", "/workspace") is None
