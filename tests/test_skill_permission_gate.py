import pytest

from zen_claw.agent.tools.filesystem import ReadFileTool, WriteFileTool
from zen_claw.agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_tool_registry_blocks_tool_not_in_skill_allowlist(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(ReadFileTool(allowed_dir=tmp_path))
    reg.register(WriteFileTool(allowed_dir=tmp_path))
    reg.set_skill_attribution(["foo"], mode="enforce")
    reg.set_skill_allowed_tools({"read_file"})

    res = await reg.execute(
        "write_file", {"path": str(tmp_path / "x.txt"), "content": "x"}, trace_id="t1"
    )
    assert res.ok is False
    assert res.error is not None
    assert res.error.kind.value == "permission"
    assert res.error.code == "skill_permission_denied"
    assert res.meta.get("policy_scope") == "skill"
    assert res.meta.get("skill_names") == ["foo"]
    assert res.meta.get("skill_permissions_mode") == "enforce"


@pytest.mark.asyncio
async def test_tool_registry_allows_tool_in_skill_allowlist(tmp_path) -> None:
    reg = ToolRegistry()
    reg.register(ReadFileTool(allowed_dir=tmp_path))
    reg.set_skill_attribution(["foo"], mode="enforce")
    reg.set_skill_allowed_tools({"read_file"})

    res = await reg.execute("read_file", {"path": str(tmp_path / "missing.txt")}, trace_id="t2")
    assert res.ok is False
    assert res.error is not None
    # Not blocked by skill gate; fails because file is missing.
    assert res.error.code != "skill_permission_denied"
