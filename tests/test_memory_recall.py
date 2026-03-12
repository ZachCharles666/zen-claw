from pathlib import Path

from zen_claw.agent.context import ContextBuilder
from zen_claw.agent.memory import MemoryStore
from zen_claw.agent.memory_recall import MemoryRecallStrategy


class ConstantRecall(MemoryRecallStrategy):
    def score(self, query: str, candidate: str) -> float:
        return 1.0 if candidate else 0.0


def _disable_skills(ctx: ContextBuilder) -> None:
    """Avoid coupling memory tests to external/builtin skill files."""
    ctx.skills.get_always_skills = lambda: []
    ctx.skills.build_skills_summary = lambda: ""
    ctx.skills.load_skills_for_context = lambda names: ""


def test_relevant_memory_context_keyword_recall(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "- User prefers Go for new high-performance modules.\n"
        "- User dislikes unnecessary verbosity.\n"
    )
    store.append_today("- Working on Rust benchmark today.")

    out = store.get_relevant_memory_context("prioritize go performance", days=3)
    assert "## Relevant Memory" in out
    assert "prefers Go" in out


def test_relevant_memory_context_supports_custom_strategy(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, recall_strategy=ConstantRecall())
    store.write_long_term("- alpha\n- beta\n")
    out = store.get_relevant_memory_context("anything", max_items=1, max_chars=100)
    assert out.count("- ") == 1


def test_context_builder_uses_query_relevant_memory_first(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")
    _disable_skills(ctx)
    ctx.memory.write_long_term(
        "- Project codename is zen_claw.\n- Favorite editor theme is ocean.\n"
    )
    messages = ctx.build_messages(history=[], current_message="what is the project codename?")
    system = str(messages[0]["content"])
    assert "## Relevant Memory" in system
    assert "codename is zen_claw" in system


def test_relevant_memory_context_deduplicates_entries(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "- User prefers Go modules for performance.\n- User prefers Go modules for performance.\n"
    )
    store.append_today("- User prefers Go modules for performance.")

    out = store.get_relevant_memory_context("go performance", days=7, max_items=8, max_chars=800)
    assert out.count("User prefers Go modules for performance.") == 1


def test_context_builder_with_recall_mode_none_skips_relevant_lookup(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="none")
    _disable_skills(ctx)
    ctx.memory.write_long_term("- Project codename is zen_claw.\n")
    messages = ctx.build_messages(history=[], current_message="what is codename?")
    system = str(messages[0]["content"])
    assert "## Relevant Memory" not in system
    assert "## Long-term Memory" in system


def test_context_builder_with_recall_mode_none_does_not_call_relevant_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="none")
    _disable_skills(ctx)

    def _boom(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(ctx.memory, "get_relevant_memory_context", _boom)
    _ = ctx.build_messages(history=[], current_message="query")


def test_context_builder_with_recall_mode_recent_prefers_recent_memory(
    tmp_path: Path, monkeypatch
) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="recent")
    _disable_skills(ctx)
    ctx.memory.write_long_term("- long-term only fact.\n")
    ctx.memory.append_today("- recent sprint note.\n")

    called = {"relevant": 0}

    def _track(*args, **kwargs):
        called["relevant"] += 1
        return ""

    monkeypatch.setattr(ctx.memory, "get_relevant_memory_context", _track)
    messages = ctx.build_messages(history=[], current_message="any query")
    system = str(messages[0]["content"])
    assert "## Recent Memory" in system
    assert "recent sprint note" in system
    assert called["relevant"] == 0


def test_context_builder_includes_tool_learning_section(tmp_path: Path) -> None:
    ctx = ContextBuilder(tmp_path, memory_recall_mode="keyword")
    _disable_skills(ctx)
    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    learning_file.parent.mkdir(parents=True, exist_ok=True)
    learning_file.write_text(
        '# Tool Learning\n\n- tool=read_file from={"file":"a"} to={"path":"a"}\n',
        encoding="utf-8",
    )

    messages = ctx.build_messages(history=[], current_message="continue")
    system = str(messages[0]["content"])
    assert "# Tool Corrections" in system
    assert "tool=read_file" in system


def test_tool_learning_context_is_bounded(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    learning_file.parent.mkdir(parents=True, exist_ok=True)
    learning_file.write_text("X" * 2000, encoding="utf-8")

    out = store.get_tool_learning_context(max_chars=120)
    assert out.startswith("## Tool Learning")
    assert len(out) <= 160


def test_tool_learning_context_prefers_query_relevant_entry(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    learning_file.parent.mkdir(parents=True, exist_ok=True)
    learning_file.write_text(
        "# Tool Learning\n\n"
        '- 2026-02-17T10:00:00Z tool=read_file sig=a1 error="missing path" from={"file":"a.txt"} to={"path":"a.txt"} trace_id=t1\n'
        '- 2026-02-17T10:01:00Z tool=dummy_search sig=b2 error="missing query" from={"q":"openclaw"} to={"query":"openclaw"} trace_id=t2\n',
        encoding="utf-8",
    )

    out = store.get_tool_learning_context(query="dummy_search openclaw", max_items=1, max_chars=500)
    assert "tool=dummy_search" in out
    assert "tool=read_file" not in out


def test_suggest_tool_arg_rewrite_matches_exact_failed_args(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    learning_file = tmp_path / "memory" / "TOOLS_LEARNING.md"
    learning_file.parent.mkdir(parents=True, exist_ok=True)
    learning_file.write_text(
        "# Tool Learning\n\n"
        '- 2026-02-17T10:01:00Z tool=dummy_search sig=b2 error="missing query" from={"q":"openclaw"} to={"query":"openclaw"} trace_id=t2\n',
        encoding="utf-8",
    )

    rewrite = store.suggest_tool_arg_rewrite(
        "dummy_search", {"q": "openclaw"}, query="dummy_search"
    )
    assert rewrite == {"query": "openclaw"}

    no_rewrite = store.suggest_tool_arg_rewrite(
        "dummy_search", {"q": "other"}, query="dummy_search"
    )
    assert no_rewrite is None
