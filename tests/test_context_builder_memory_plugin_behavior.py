from pathlib import Path

from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.memory_recall import KeywordRecallStrategy


def test_keyword_mode_prefers_relevant_memory_over_full_context(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")
    ctx.memory.write_long_term("- likes apples\n- likes bananas\n")

    prompt = ctx.build_system_prompt(memory_query="apples")
    assert "# Memory" in prompt
    assert "## Relevant Memory" in prompt
    assert "- likes apples" in prompt
    assert "## Long-term Memory" not in prompt


def test_keyword_mode_falls_back_to_full_context_when_no_match(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")
    ctx.memory.write_long_term("- likes apples\n- likes bananas\n")

    prompt = ctx.build_system_prompt(memory_query="oranges")
    assert "# Memory" in prompt
    assert "## Relevant Memory" not in prompt
    assert "## Long-term Memory" in prompt
    assert "likes apples" in prompt


def test_recent_mode_injects_recent_memory_block(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="recent")
    ctx.memory.append_today("- met Alice")

    prompt = ctx.build_system_prompt(memory_query="anything")
    assert "# Memory" in prompt
    assert "## Recent Memory" in prompt
    assert "met Alice" in prompt


def test_none_mode_does_not_use_query_ranked_memory(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="none")
    ctx.memory.write_long_term("- likes apples\n")

    prompt = ctx.build_system_prompt(memory_query="apples")
    assert "# Memory" in prompt
    assert "## Relevant Memory" not in prompt
    assert "## Recent Memory" not in prompt
    assert "## Long-term Memory" in prompt


def test_default_mode_is_sqlite(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path)
    assert ctx.memory_recall_mode == "sqlite"


def test_sqlite_mode_falls_back_to_keyword_on_runtime_error(tmp_path: Path, monkeypatch) -> None:
    class _BrokenSqliteRecall:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("sqlite init failed")

    monkeypatch.setattr(
        "zen_claw.agent.memory_sqlite.SqliteVectorRecallStrategy",
        _BrokenSqliteRecall,
    )
    ctx = ContextBuilder(tmp_path, memory_recall_mode="sqlite")
    assert isinstance(ctx.memory.recall_strategy, KeywordRecallStrategy)
