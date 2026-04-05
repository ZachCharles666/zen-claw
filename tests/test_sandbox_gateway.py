import pytest

from zen_claw.agent.tools.gateway import GatewayTool
from zen_claw.agent.tools.shell import ExecTool


@pytest.fixture
def mock_exec_tool():
    """Returns a basic sidecar ExecTool for gateway testing."""
    return ExecTool(mode="sidecar", sidecar_url="http://127.0.0.1:4488/v1/exec")


@pytest.mark.asyncio
async def test_gateway_routes_command_to_sidecar_backend(tmp_path, mock_exec_tool):
    called: dict[str, object] = {}

    async def _fake_execute(**kwargs):  # type: ignore[no-untyped-def]
        called.update(kwargs)
        from zen_claw.agent.tools.result import ToolResult

        return ToolResult.success("ok")

    mock_exec_tool.execute = _fake_execute  # type: ignore[method-assign]
    gateway = GatewayTool(backend_tool=mock_exec_tool)
    result = await gateway.execute("echo test", working_dir=str(tmp_path))
    assert result.ok
    assert called["command"] == "echo test"
    assert called["working_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_gateway_no_longer_uses_local_builtin_fallback(mock_exec_tool):
    async def _fake_execute(**kwargs):  # type: ignore[no-untyped-def]
        from zen_claw.agent.tools.result import ToolErrorKind, ToolResult

        del kwargs
        return ToolResult.failure(
            ToolErrorKind.RUNTIME,
            "exec failed",
            code="exec_failed",
        )

    mock_exec_tool.execute = _fake_execute  # type: ignore[method-assign]
    gateway = GatewayTool(backend_tool=mock_exec_tool)
    result = await gateway.execute("set")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "exec_failed"
