"""Tests for AgentRouteStore bulk-management methods and the new management API endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient as _TC
    from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key
    import zen_claw.dashboard.server as _srv_mod

    _FASTAPI_AVAILABLE = api_app is not None
except Exception:
    _FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path):
    from zen_claw.channels.routing import AgentRouteStore

    return AgentRouteStore(tmp_path / "routes.db")


def _bind(store, channel: str, chat_id: str, user_id: str, agent_id: str):
    store.set_route(channel=channel, chat_id=chat_id, user_id=user_id, agent_id=agent_id)


# ---------------------------------------------------------------------------
# list_routes
# ---------------------------------------------------------------------------


class TestListRoutes:
    def test_list_routes_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.list_routes() == []

    def test_list_routes_after_bind(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        routes = store.list_routes()
        assert len(routes) == 1
        assert routes[0]["channel"] == "feishu"
        assert routes[0]["agent_id"] == "finance"
        assert routes[0]["chat_id"] == "C1"
        assert routes[0]["user_id"] == "U1"

    def test_list_routes_filter_by_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "default")
        feishu_routes = store.list_routes(channel="feishu")
        assert len(feishu_routes) == 1
        assert feishu_routes[0]["channel"] == "feishu"

    def test_list_routes_filter_by_agent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "default")
        _bind(store, "telegram", "C3", "U3", "finance")
        finance_routes = store.list_routes(agent_id="finance")
        assert len(finance_routes) == 2
        assert all(r["agent_id"] == "finance" for r in finance_routes)

    def test_list_routes_pagination(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for i in range(5):
            _bind(store, "feishu", f"C{i}", f"U{i}", "finance")
        first = store.list_routes(limit=2, offset=0)
        second = store.list_routes(limit=2, offset=2)
        assert len(first) == 2
        assert len(second) == 2
        keys_first = {r["route_key"] for r in first}
        keys_second = {r["route_key"] for r in second}
        assert keys_first.isdisjoint(keys_second)


# ---------------------------------------------------------------------------
# list_audit_all
# ---------------------------------------------------------------------------


class TestListAuditAll:
    def test_list_audit_all_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.list_audit_all() == []

    def test_list_audit_all_after_multiple_routes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "default")
        audit = store.list_audit_all()
        assert len(audit) >= 2
        channels = {r["channel"] for r in audit}
        assert "feishu" in channels
        assert "webchat" in channels

    def test_list_audit_all_filter_by_agent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "default")
        audit = store.list_audit_all(agent_id="finance")
        assert all(
            r["new_agent_id"] == "finance" or r["previous_agent_id"] == "finance"
            for r in audit
        )
        assert len(audit) >= 1

    def test_list_audit_all_filter_by_reason(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        # clear creates a "manual_clear" audit entry
        store.clear_route(channel="feishu", chat_id="C1", user_id="U1", reason="manual_clear")
        bind_audit = store.list_audit_all(reason="manual_bind")
        clear_audit = store.list_audit_all(reason="manual_clear")
        assert all(r["reason"] == "manual_bind" for r in bind_audit)
        assert all(r["reason"] == "manual_clear" for r in clear_audit)
        assert len(bind_audit) >= 1
        assert len(clear_audit) >= 1

    def test_list_audit_all_filter_by_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "default")
        feishu_audit = store.list_audit_all(channel="feishu")
        assert all(r["channel"] == "feishu" for r in feishu_audit)


# ---------------------------------------------------------------------------
# clear_routes_by
# ---------------------------------------------------------------------------


class TestClearRoutesBy:
    def test_clear_routes_requires_filter(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="At least one"):
            store.clear_routes_by()

    def test_clear_routes_by_agent_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "finance")
        result = store.clear_routes_by(agent_id="finance", dry_run=True)
        assert result["cleared"] == 0
        assert result["dry_run"] is True
        assert len(result["preview"]) == 2
        # routes must still exist
        assert len(store.list_routes(agent_id="finance")) == 2

    def test_clear_routes_by_agent(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "webchat", "C2", "U2", "finance")
        _bind(store, "telegram", "C3", "U3", "default")
        result = store.clear_routes_by(agent_id="finance")
        assert result["cleared"] == 2
        assert result["dry_run"] is False
        # finance routes gone, default route remains
        assert store.list_routes(agent_id="finance") == []
        assert len(store.list_routes(agent_id="default")) == 1

    def test_clear_routes_by_channel(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        _bind(store, "feishu", "C2", "U2", "default")
        _bind(store, "webchat", "C3", "U3", "finance")
        result = store.clear_routes_by(channel="feishu")
        assert result["cleared"] == 2
        # webchat route survives
        remaining = store.list_routes()
        assert len(remaining) == 1
        assert remaining[0]["channel"] == "webchat"

    def test_clear_routes_by_writes_audit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _bind(store, "feishu", "C1", "U1", "finance")
        store.clear_routes_by(agent_id="finance", reason="decommission")
        audit = store.list_audit_all(reason="decommission")
        assert len(audit) >= 1


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


def _make_route_store_fixture(tmp_path: Path):
    """Create a small route store with test data."""
    from zen_claw.channels.routing import AgentRouteStore

    store = AgentRouteStore(tmp_path / "routes.db")
    store.set_route(channel="feishu", chat_id="C1", user_id="U1", agent_id="finance")
    store.set_route(channel="webchat", chat_id="C2", user_id="U2", agent_id="default")
    return store


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


class TestAgentRoutesAPI:
    """API-level tests using the existing api_app fixture pattern."""

    def test_list_routes_api_empty(self, tmp_path, monkeypatch):
        """GET /api/v1/agents/routes on empty store returns empty list."""
        _skip_no_fastapi()
        from zen_claw.channels.routing import AgentRouteStore

        empty_store = AgentRouteStore(tmp_path / "empty.db")
        monkeypatch.setattr(_srv_mod, "_ensure_agent_route_store", lambda cfg: empty_store)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/routes", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert data["routes"] == []
        assert data["total"] == 0

    def test_bulk_clear_missing_filter_returns_400(self, tmp_path, monkeypatch):
        """DELETE /api/v1/agents/routes without channel or agent_id → 400."""
        _skip_no_fastapi()
        from zen_claw.channels.routing import AgentRouteStore

        store = AgentRouteStore(tmp_path / "r.db")
        monkeypatch.setattr(_srv_mod, "_ensure_agent_route_store", lambda cfg: store)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.request(
            "DELETE",
            "/api/v1/agents/routes",
            json={},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 400

    def test_bulk_clear_dry_run(self, tmp_path, monkeypatch):
        """DELETE /api/v1/agents/routes with dry_run=true previews without clearing."""
        _skip_no_fastapi()

        store = _make_route_store_fixture(tmp_path)
        monkeypatch.setattr(_srv_mod, "_ensure_agent_route_store", lambda cfg: store)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.request(
            "DELETE",
            "/api/v1/agents/routes",
            json={"agent_id": "finance", "dry_run": True},
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True
        assert data["cleared_count"] == 0
        assert len(data["preview_routes"]) >= 1
        # store untouched
        assert len(store.list_routes(agent_id="finance")) >= 1

    def test_routes_audit_api(self, tmp_path, monkeypatch):
        """GET /api/v1/agents/routes/audit returns audit list."""
        _skip_no_fastapi()

        store = _make_route_store_fixture(tmp_path)
        monkeypatch.setattr(_srv_mod, "_ensure_agent_route_store", lambda cfg: store)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/agents/routes/audit", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "audit" in data
        assert isinstance(data["audit"], list)

    def test_routes_audit_csv_export(self, tmp_path, monkeypatch):
        """GET /api/v1/agents/routes/audit?format=csv returns CSV."""
        _skip_no_fastapi()

        store = _make_route_store_fixture(tmp_path)
        monkeypatch.setattr(_srv_mod, "_ensure_agent_route_store", lambda cfg: store)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/agents/routes/audit?format=csv",
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
