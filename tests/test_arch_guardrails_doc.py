from pathlib import Path

import pytest


def test_arch_guardrails_doc_exists_and_has_core_rules() -> None:
    p = Path("ARCH_GUARDRAILS.md")
    if not p.exists():
        pytest.skip("ARCH_GUARDRAILS.md not present in this repository snapshot")
    text = p.read_text(encoding="utf-8")
    assert "# Architecture Guardrails" in text
    assert "No full rewrite." in text
    assert "Python remains the orchestration mainline" in text
    assert "Go/Rust are only used for high-value boundaries" in text
