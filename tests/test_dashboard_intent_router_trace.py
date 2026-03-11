import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from zen_claw.agent.loop import AgentLoop
from zen_claw.bus.queue import MessageBus
from zen_claw.dashboard.server import build_dashboard_snapshot
from zen_claw.providers.base import LLMProvider


class _FailIfCalledProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        raise AssertionError("LLM chat should not be called for direct dashboard trace test")

    def get_default_model(self) -> str:
        return "fake-model"


def test_process_direct_appends_intent_router_dashboard_event(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FailIfCalledProvider(),
        workspace=tmp_path,
        model="fake-model",
        enable_planning=False,
    )
    loop.sessions.sessions_dir = tmp_path / "sessions"
    loop.sessions.sessions_dir.mkdir(parents=True, exist_ok=True)
    loop._extract_and_store_memory = AsyncMock()  # type: ignore[method-assign]

    out = asyncio.run(loop.process_direct("请告诉我纽约现在几点"))

    assert "纽约当前时间：" in out
    log_path = data_dir / "dashboard" / "intent_router.log.jsonl"
    assert log_path.exists()
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["intent_name"] == "time"
    assert rows[-1]["route_status"] == "direct_success"
    assert rows[-1]["handled"] is True


def test_build_dashboard_snapshot_includes_intent_router_events(
    monkeypatch, tmp_path: Path
) -> None:
    from zen_claw.config.schema import Config

    cfg = Config()
    data_dir = tmp_path / "data"
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    (data_dir / "channels").mkdir(parents=True, exist_ok=True)
    (data_dir / "nodes").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)
    (data_dir / "cron" / "jobs.json").write_text('{"jobs":[]}', encoding="utf-8")
    (data_dir / "channels" / "rate_limit_stats.json").write_text(
        '{"channels":{}}', encoding="utf-8"
    )
    (data_dir / "nodes" / "state.json").write_text(
        '{"version":1,"nodes":{},"tasks":[],"approval_events":[]}',
        encoding="utf-8",
    )
    (data_dir / "dashboard" / "intent_router.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 123,
                "trace_id": "trace-1",
                "channel": "cli",
                "chat_id": "direct",
                "intent_name": "weather",
                "route_status": "direct_failed",
                "handled": True,
                "diagnostic": "weather_days_exceed_limit:70",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    snapshot = build_dashboard_snapshot(cfg)

    assert len(snapshot["agent"]["intent_router_events"]) == 1
    assert snapshot["agent"]["intent_router_events"][0]["intent_name"] == "weather"
    assert snapshot["agent"]["intent_router_events"][0]["route_status"] == "direct_failed"
    assert snapshot["agent"]["intent_router_summary"]["total"] == 1
    assert snapshot["agent"]["intent_router_summary"]["direct_failed"] == 1
    assert snapshot["agent"]["intent_router_summary"]["direct_success"] == 0
