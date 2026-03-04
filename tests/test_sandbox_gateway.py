import os

import pytest

from zen_claw.agent.tools.gateway import GatewayTool
from zen_claw.agent.tools.result import ToolErrorKind
from zen_claw.agent.tools.shell import ExecTool


@pytest.fixture
def mock_exec_tool():
    """Returns a basic local ExecTool for testing."""
    return ExecTool(mode="local")

@pytest.mark.asyncio
async def test_gateway_directory_isolation(tmp_path, mock_exec_tool):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = GatewayTool(backend_tool=mock_exec_tool, workspace=str(workspace))

    # Executing normally within sandbox is allowed
    # Note: gateway automatically resolves working_dir to .sandbox if none provided
    result = await gateway.execute("echo test")
    assert result.ok

    # Executing outside the sandbox should be blocked
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    result = await gateway.execute("echo test", working_dir=str(outside_dir))
    assert not result.ok
    assert result.error.kind == ToolErrorKind.PERMISSION
    assert result.error.code == "gateway_path_traversal"

@pytest.mark.asyncio
async def test_gateway_environment_sanitization(tmp_path, mock_exec_tool):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gateway = GatewayTool(backend_tool=mock_exec_tool, workspace=str(workspace))

    # Set a sensitive env var
    # Test checking environment in linux/unix (printenv or env), but on Windows it's 'set'
    os.environ["SUPER_SECRET_KEY"] = "hidden_value123"

    # We execute a shell command to print env vars
    # In Windows pwsh/cmd it would be `set`
    result = await gateway.execute("set")

    assert result.ok
    # The output should NOT contain SUPER_SECRET_KEY
    assert "SUPER_SECRET_KEY" not in result.content
    assert "hidden_value123" not in result.content

    # But it should contain safe vars like PATH
    assert "PATH=" in result.content or "Path=" in result.content
