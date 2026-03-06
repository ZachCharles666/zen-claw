import asyncio
from pathlib import Path

import pytest

from zen_claw.agent.loop import AgentLoop
from zen_claw.agent.skills import SkillsLoader
from zen_claw.bus.events import InboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.providers.base import LLMProvider, LLMResponse
from zen_claw.skills.registry import RegistryEntry, SkillsRegistry


@pytest.fixture(autouse=True)
def mock_skills_loader(monkeypatch):
    # Mock mapping and time to prevent potentially slow/hanging I/O or crypto in CI
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)


class _DummyProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy"


def test_search_skill_generates_signed_snapshot(tmp_path: Path, monkeypatch) -> None:
    loader = SkillsLoader(tmp_path)
    monkeypatch.setattr(loader, "_now_ts", lambda: 1_700_000_000.0)
    monkeypatch.setattr(
        loader,
        "_search_registry",
        lambda query: [
            RegistryEntry(
                name="web-search",
                version="1.0.0",
                description="x",
                author="team",
                download_url="https://downloads.example.com/web-search.zip",
                sha256="abc123",
            )
        ],
    )

    rows = asyncio.run(loader.search_skill("web"))
    assert len(rows) == 1
    snapshot_id = rows[0]["snapshot_id"]
    snapshot = loader._snapshots[snapshot_id]
    assert snapshot["issued_at"] == 1_700_000_000.0
    assert snapshot["expires_at"] == 1_700_003_600.0
    assert snapshot_id == loader._sign_snapshot(snapshot)


def test_install_skill_by_snapshot_rejects_invalid_signature(tmp_path: Path, monkeypatch) -> None:
    loader = SkillsLoader(tmp_path)
    monkeypatch.setattr(loader, "_now_ts", lambda: 1000.0)
    loader._snapshots["tampered"] = {
        "name": "web-search",
        "version": "1.0.0",
        "digest": "abc123",
        "publisher": "team",
        "download_url": "https://downloads.example.com/web-search.zip",
        "issued_at": 900.0,
        "expires_at": 2000.0,
        "nonce": "n1",
    }

    ok, msg = asyncio.run(loader.install_skill_by_snapshot("tampered"))
    assert ok is False
    assert "invalid snapshot signature" in msg


def test_registry_fetch_rejects_unsafe_host(monkeypatch, tmp_path: Path) -> None:
    registry = SkillsRegistry(
        registry_url="https://registry.example.com/index.json",
        cache_path=tmp_path / "registry_cache.json",
        cache_ttl_sec=0,
    )
    monkeypatch.setattr("zen_claw.skills.registry.resolve_safe_ip", lambda host: None)
    with pytest.raises(RuntimeError, match="unsafe"):
        registry.fetch(force=True)


def test_process_message_fail_closed_when_channel_role_missing(tmp_path: Path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
    )
    msg = InboundMessage(
        channel="whatsapp",
        sender_id="u1",
        chat_id="c1",
        content="hello",
        metadata={"identity_verified": True},
    )
    out = asyncio.run(loop._process_message(msg))
    assert out is not None
    assert "Access Denied" in out.content
    assert provider.calls == 0


def test_process_message_fail_closed_when_tenant_role_incomplete(tmp_path: Path) -> None:
    provider = _DummyProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
    )
    msg = InboundMessage(
        channel="whatsapp",
        sender_id="u1",
        chat_id="c1",
        content="hello",
        metadata={"channel_role": "user", "tenant_id": "t1"},
    )
    out = asyncio.run(loop._process_message(msg))
    assert out is not None
    assert "Access Denied" in out.content
    assert provider.calls == 0
