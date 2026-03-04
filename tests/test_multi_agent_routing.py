from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zen_claw.bus.queue import MessageBus
from zen_claw.channels.manager import ChannelManager
from zen_claw.channels.routing import AgentRouteStore
from zen_claw.config.schema import Config


def test_route_key_format_and_resolve(tmp_path: Path) -> None:
    store = AgentRouteStore(tmp_path / "routes.db")
    key = store.make_route_key("slack", "C1", "U1")
    assert key == "slack:C1:U1"
    store.set_route(channel="slack", chat_id="C1", user_id="U1", agent_id="agent-a")
    route = store.resolve_route(channel="slack", chat_id="C1", user_id="U1")
    assert route is not None
    assert route.agent_id == "agent-a"


def test_route_lww_and_audit_50_writes(tmp_path: Path) -> None:
    store = AgentRouteStore(tmp_path / "routes.db")
    base_ms = 1_700_000_000_000

    def _write(i: int) -> None:
        store.set_route(
            channel="telegram",
            chat_id="chat-1",
            user_id="user-1",
            agent_id=f"agent-{i}",
            reason="load_test",
            at_ms=base_ms + i,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        for i in range(50):
            pool.submit(_write, i)

    route = store.resolve_route(channel="telegram", chat_id="chat-1", user_id="user-1")
    assert route is not None
    assert route.agent_id == "agent-49"
    audit = store.list_audit(channel="telegram", chat_id="chat-1", user_id="user-1")
    assert len(audit) == 50
    assert audit[-1]["new_agent_id"] == "agent-49"


def test_soft_rollback_within_grace_window(tmp_path: Path) -> None:
    store = AgentRouteStore(tmp_path / "routes.db")
    base_ms = 1_700_000_000_000
    store.set_route(
        channel="webchat",
        chat_id="sess-1",
        user_id="u1",
        agent_id="agent-old",
        at_ms=base_ms,
    )
    store.set_route(
        channel="webchat",
        chat_id="sess-1",
        user_id="u1",
        agent_id="agent-bad",
        at_ms=base_ms + 1000,
    )
    rolled = store.soft_rollback_on_error(
        channel="webchat",
        chat_id="sess-1",
        user_id="u1",
        current_agent_id="agent-bad",
        grace_period_ms=180_000,
        now_ms=base_ms + 2000,
    )
    assert rolled is not None
    assert rolled.agent_id == "agent-old"


def test_soft_rollback_outside_grace_window(tmp_path: Path) -> None:
    store = AgentRouteStore(tmp_path / "routes.db")
    base_ms = 1_700_000_000_000
    store.set_route(channel="discord", chat_id="c1", user_id="u1", agent_id="a1", at_ms=base_ms)
    store.set_route(channel="discord", chat_id="c1", user_id="u1", agent_id="a2", at_ms=base_ms + 1)
    rolled = store.soft_rollback_on_error(
        channel="discord",
        chat_id="c1",
        user_id="u1",
        current_agent_id="a2",
        grace_period_ms=50,
        now_ms=base_ms + 1000,
    )
    assert rolled is None
    current = store.resolve_route(channel="discord", chat_id="c1", user_id="u1")
    assert current is not None
    assert current.agent_id == "a2"


def test_gc_expired_routes(tmp_path: Path) -> None:
    store = AgentRouteStore(tmp_path / "routes.db")
    now = 1_700_000_200_000
    store.set_route(channel="signal", chat_id="c1", user_id="u1", agent_id="a1", at_ms=now - 100_000)
    store.set_route(channel="signal", chat_id="c1", user_id="u2", agent_id="a2", at_ms=now - 1_000)
    deleted = store.gc_expired_routes(ttl_ms=10_000, now_ms=now)
    assert deleted == 1
    assert store.resolve_route(channel="signal", chat_id="c1", user_id="u1") is None
    assert store.resolve_route(channel="signal", chat_id="c1", user_id="u2") is not None


def test_channel_manager_route_bind_resolve_and_rollback(tmp_path: Path) -> None:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    mgr = ChannelManager(cfg, MessageBus())
    bound = mgr.bind_agent(channel="webchat", chat_id="s1", user_id="u1", agent_id="agent-a")
    assert bound == "agent-a"
    assert mgr.resolve_agent(channel="webchat", chat_id="s1", user_id="u1") == "agent-a"
    mgr.bind_agent(channel="webchat", chat_id="s1", user_id="u1", agent_id="agent-b")
    rolled = mgr.mark_agent_error(channel="webchat", chat_id="s1", user_id="u1", current_agent_id="agent-b")
    assert rolled == "agent-a"
