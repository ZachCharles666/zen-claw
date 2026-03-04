from pathlib import Path


def test_release_gate_defaults_enable_loc_gate_with_skip_override() -> None:
    p = Path("scripts/release_gate.ps1")
    text = p.read_text(encoding="utf-8")
    assert "[switch]$SkipLocGate" in text
    assert "if (-not $SkipLocGate -or $FailOnLocIncrease)" in text
