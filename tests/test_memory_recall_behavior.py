from datetime import datetime
from pathlib import Path

from zen_claw.agent.memory import MemoryStore


def test_relevant_memory_prefers_recent_when_scores_tie(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("- use go for performance modules")
    today = datetime.now().date().strftime("%Y-%m-%d")
    (store.memory_dir / f"{today}.md").write_text(
        "# today\n\n- use go for performance modules in current sprint",
        encoding="utf-8",
    )

    out = store.get_relevant_memory_context(
        "go performance modules", days=3, max_items=1, max_chars=200
    )
    # recent entry should win tie via small recency bonus
    assert "current sprint" in out


def test_relevant_memory_respects_max_items_and_chars(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term(
        "- alpha go performance note\n- beta go performance note\n- gamma go performance note\n"
    )
    out = store.get_relevant_memory_context("go performance", max_items=2, max_chars=80)
    assert out.count("- ") <= 2
    assert len(out) <= 120
