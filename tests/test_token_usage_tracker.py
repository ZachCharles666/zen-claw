"""Tests for A1 — Token usage tracking and reporting."""

from pathlib import Path

from zen_claw.telemetry.token_usage import TokenUsageRecord, TokenUsageTracker

# ── TokenUsageRecord ──────────────────────────────────────────────────────────

def test_token_usage_record_total_tokens() -> None:
    rec = TokenUsageRecord(
        at_ms=1700000000000,
        agent_id="default",
        model="claude-opus-4-5",
        input_tokens=300,
        output_tokens=150,
        total_tokens=450,
    )
    assert rec.total_tokens == 450


def test_token_usage_record_date_str() -> None:
    # 2024-01-15 00:00:00 UTC = 1705276800000 ms
    rec = TokenUsageRecord(
        at_ms=1705276800000,
        agent_id="a",
        model="m",
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
    assert rec.date_str == "2024-01-15"


def test_token_usage_record_roundtrip() -> None:
    rec = TokenUsageRecord(
        at_ms=1700000000000,
        agent_id="bot",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        session_id="telegram:123",
        intent="direct",
    )
    d = rec.to_dict()
    rec2 = TokenUsageRecord.from_dict(d)
    assert rec2.agent_id == "bot"
    assert rec2.model == "gpt-4o"
    assert rec2.input_tokens == 100
    assert rec2.output_tokens == 50
    assert rec2.total_tokens == 150
    assert rec2.session_id == "telegram:123"
    assert rec2.intent == "direct"


# ── TokenUsageTracker: write and read ────────────────────────────────────────

def test_tracker_record_appends_to_jsonl(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    rec = tracker.record(
        agent_id="agent1",
        model="claude-opus-4-5",
        input_tokens=200,
        output_tokens=100,
        at_ms=1700000000000,
    )
    assert rec.total_tokens == 300

    log_path = tmp_path / "dashboard" / "token_usage.log.jsonl"
    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1


def test_tracker_read_all_returns_records(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="a1", model="m1", input_tokens=100, output_tokens=50)
    tracker.record(agent_id="a2", model="m2", input_tokens=200, output_tokens=80)

    records = tracker.read_all()
    assert len(records) == 2
    agent_ids = {r.agent_id for r in records}
    assert "a1" in agent_ids
    assert "a2" in agent_ids


def test_tracker_read_all_empty_file(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    assert tracker.read_all() == []


def test_tracker_read_all_ignores_corrupted_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "dashboard" / "token_usage.log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"agent_id":"ok","model":"m","at_ms":0,"input_tokens":1,"output_tokens":1,"total_tokens":2}\n{NOT JSON}\n')
    tracker = TokenUsageTracker(tmp_path)
    records = tracker.read_all()
    assert len(records) == 1
    assert records[0].agent_id == "ok"


# ── Summary grouping ─────────────────────────────────────────────────────────

def test_tracker_summary_by_agent(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="agent1", model="m", input_tokens=100, output_tokens=50)
    tracker.record(agent_id="agent1", model="m", input_tokens=200, output_tokens=100)
    tracker.record(agent_id="agent2", model="m", input_tokens=50, output_tokens=20)

    s = tracker.summary()
    assert s["by_agent"]["agent1"]["calls"] == 2
    assert s["by_agent"]["agent1"]["input"] == 300
    assert s["by_agent"]["agent2"]["calls"] == 1


def test_tracker_summary_by_model(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="a", model="haiku", input_tokens=50, output_tokens=20)
    tracker.record(agent_id="a", model="opus", input_tokens=300, output_tokens=100)

    s = tracker.summary()
    assert "haiku" in s["by_model"]
    assert "opus" in s["by_model"]
    assert s["by_model"]["opus"]["input"] == 300


def test_tracker_summary_total(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="a", model="m", input_tokens=100, output_tokens=50)
    tracker.record(agent_id="b", model="m", input_tokens=200, output_tokens=80)

    s = tracker.summary()
    assert s["total"]["calls"] == 2
    assert s["total"]["input"] == 300
    assert s["total"]["output"] == 130
    assert s["total"]["total"] == 430


def test_tracker_summary_since_ms_filter(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="a", model="m", input_tokens=100, output_tokens=50, at_ms=1000)
    tracker.record(agent_id="a", model="m", input_tokens=200, output_tokens=80, at_ms=2000)

    s = tracker.summary(since_ms=1500)
    assert s["total"]["calls"] == 1
    assert s["total"]["input"] == 200


# ── Budget tracking ───────────────────────────────────────────────────────────

def test_tracker_daily_total(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    # Use at_ms = 2024-01-15 UTC
    tracker.record(agent_id="bot", model="m", input_tokens=100, output_tokens=50, at_ms=1705276800000)
    tracker.record(agent_id="bot", model="m", input_tokens=100, output_tokens=50, at_ms=1705276800000)

    total = tracker.daily_total("bot", "2024-01-15")
    assert total == 300  # (150 + 150)


def test_tracker_is_over_budget_true(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="bot", model="m", input_tokens=400, output_tokens=200, at_ms=1705276800000)

    assert tracker.is_over_budget("bot", budget_daily=500, date_str="2024-01-15") is True


def test_tracker_is_over_budget_false(tmp_path: Path) -> None:
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="bot", model="m", input_tokens=100, output_tokens=50, at_ms=1705276800000)

    assert tracker.is_over_budget("bot", budget_daily=500, date_str="2024-01-15") is False


def test_tracker_unlimited_budget_never_over(tmp_path: Path) -> None:
    """budget_daily=0 means unlimited — always returns False."""
    tracker = TokenUsageTracker(tmp_path)
    tracker.record(agent_id="bot", model="m", input_tokens=10000, output_tokens=5000)

    assert tracker.is_over_budget("bot", budget_daily=0) is False


# ── Config schema ─────────────────────────────────────────────────────────────

def test_agent_defaults_has_token_budget_daily() -> None:
    from zen_claw.config.schema import AgentDefaults
    d = AgentDefaults()
    assert hasattr(d, "token_budget_daily")
    assert d.token_budget_daily == 0  # default unlimited


def test_agent_profile_config_has_token_budget_daily() -> None:
    from zen_claw.config.schema import AgentProfileConfig
    p = AgentProfileConfig()
    assert hasattr(p, "token_budget_daily")
    assert p.token_budget_daily is None  # None = inherit from defaults
