"""Tests for C2 — BM25 semantic tool selector."""

from __future__ import annotations

import pytest

from zen_claw.agent.tools.semantic_selector import bm25_select, _tokenise


def _make_tool(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": {}},
    }


# ── _tokenise ─────────────────────────────────────────────────────────────────

def test_tokenise_basic():
    tokens = _tokenise("Hello World!")
    assert tokens == ["hello", "world"]


def test_tokenise_strips_empty():
    tokens = _tokenise("  a  b  c  ")
    assert "" not in tokens


# ── bm25_select ───────────────────────────────────────────────────────────────

def test_bm25_returns_all_when_fewer_than_top_k():
    tools = [_make_tool("weather", "Get weather info")]
    result = bm25_select(tools, "what is the weather", top_k=10)
    assert result == tools


def test_bm25_filters_to_top_k():
    tools = [_make_tool(f"tool_{i}", f"description number {i}") for i in range(20)]
    result = bm25_select(tools, "some query", top_k=5)
    assert len(result) == 5


def test_bm25_relevant_tool_ranked_higher():
    tools = [
        _make_tool("calculator", "Perform arithmetic calculations"),
        _make_tool("weather", "Get weather forecasts"),
        _make_tool("translator", "Translate text between languages"),
        _make_tool("calendar", "Manage calendar events"),
        _make_tool("email", "Send and read emails"),
        _make_tool("file_reader", "Read files from disk"),
        _make_tool("web_search", "Search the web"),
        _make_tool("database", "Query a database"),
        _make_tool("scheduler", "Schedule tasks"),
        _make_tool("notifier", "Send notifications"),
        _make_tool("crypto", "Encrypt and decrypt data"),
        _make_tool("math_solver", "Solve mathematical equations"),
    ]
    result = bm25_select(tools, "calculate arithmetic", top_k=3)
    names = {t["function"]["name"] for t in result}
    # Both "calculator" and "math_solver" mention arithmetic/math — at least one should appear
    assert "calculator" in names or "math_solver" in names


def test_bm25_preserves_original_order():
    """Returned tools should appear in their original list order."""
    tools = [_make_tool(f"t{i}", f"tool description {i}") for i in range(15)]
    result = bm25_select(tools, "tool description", top_k=5)
    # Check that their indices are non-decreasing relative to original list
    indices = [tools.index(t) for t in result]
    assert indices == sorted(indices)


def test_bm25_empty_query_returns_first_k():
    tools = [_make_tool(f"t{i}", f"desc {i}") for i in range(15)]
    result = bm25_select(tools, "", top_k=5)
    assert len(result) == 5


# ── ToolRegistry integration ──────────────────────────────────────────────────

def test_registry_query_hint_filters_tools():
    """query_hint triggers BM25 filtering when above threshold."""
    from zen_claw.agent.tools.registry import ToolRegistry
    from zen_claw.agent.tools.base import Tool
    from zen_claw.agent.tools.result import ToolResult

    def _make_concrete_tool(tool_name: str, tool_desc: str):
        class _T(Tool):
            @property
            def name(self) -> str:
                return tool_name
            @property
            def description(self) -> str:
                return tool_desc
            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}
            async def execute(self, **kwargs):
                return ToolResult(content=tool_name)
        return _T()

    registry = ToolRegistry()

    # Register 15 dummy tools (> default threshold of 12)
    for i in range(15):
        registry.register(_make_concrete_tool(f"tool_{i}", f"dummy tool {i}"))

    # Register one very specific tool
    registry.register(_make_concrete_tool(
        "calculator_special",
        "perform arithmetic calculation and evaluation",
    ))

    # Without query_hint → all 16 tools
    all_defs = registry.get_visible_definitions()
    assert len(all_defs) == 16

    # With query_hint → filtered to top 10 (default semantic_top_k)
    filtered = registry.get_visible_definitions(
        query_hint="calculate arithmetic", semantic_threshold=12
    )
    assert len(filtered) <= 10
