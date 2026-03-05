from pathlib import Path

from zen_claw.agent.tools.filesystem import ReadFileTool


async def test_read_file_blocks_prefix_bypass_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "work"
    outside_similar_prefix = tmp_path / "workbench"
    allowed.mkdir()
    outside_similar_prefix.mkdir()

    target = outside_similar_prefix / "secret.txt"
    target.write_text("nope", encoding="utf-8")

    tool = ReadFileTool(allowed_dir=allowed)
    result = await tool.execute(path=str(target))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "path_outside_workspace"
    assert "outside allowed directory" in result.error.message
