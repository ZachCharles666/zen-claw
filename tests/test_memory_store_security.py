from pathlib import Path

from zen_claw.agent.memory import MemoryStore


def test_memory_context_applies_size_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    long_text = "A" * 500
    today_text = "B" * 500
    store.write_long_term(long_text)
    store.append_today(today_text)

    ctx = store.get_memory_context(max_chars=300)
    assert len(ctx) <= 420  # header text + bounded content
    assert "## Long-term Memory" in ctx


def test_recent_memories_respects_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    f1 = store.memory_dir / "2026-02-14.md"
    f2 = store.memory_dir / "2026-02-13.md"
    f1.write_text("X" * 120, encoding="utf-8")
    f2.write_text("Y" * 120, encoding="utf-8")

    text = store.get_recent_memories(days=7, max_chars=150)
    assert len(text) <= 170


def test_list_memory_files_only_within_memory_dir(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    safe = store.memory_dir / "2026-02-14.md"
    safe.write_text("ok", encoding="utf-8")
    outside = tmp_path / "2026-02-13.md"
    outside.write_text("outside", encoding="utf-8")

    files = store.list_memory_files()
    assert safe in files
    assert outside not in files


def test_is_safe_memory_file_rejects_outside_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    assert store._is_safe_memory_file(outside) is False


def test_ensure_safe_write_target_rejects_outside_parent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    outside = tmp_path.parent / "oops.md"
    try:
        store._ensure_safe_write_target(outside)
        assert False, "expected PermissionError"
    except PermissionError:
        pass
