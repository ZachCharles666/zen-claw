"""Tests for API gateway and web chat endpoints."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from starlette.requests import Request

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from zen_claw.bus.events import OutboundMessage
from zen_claw.channels.routing import AgentRouteStore
from zen_claw.config.schema import Config
from zen_claw.dashboard.server import (
    _resolve_rag_tenant_id,
    api_app,
    generate_api_key,
    store_api_key,
)


@pytest.fixture
def client():
    if TestClient is None or api_app is None:
        pytest.skip("fastapi is not available")
    return TestClient(api_app)


@pytest.fixture
def valid_key(tmp_path, monkeypatch):
    from zen_claw.dashboard import server as srv

    monkeypatch.setattr(srv, "_api_keys_file", lambda: tmp_path / "api_keys.json")
    raw, _ = generate_api_key()
    store_api_key(raw)
    return raw


def test_health_is_public(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_info_requires_auth(client):
    resp = client.get("/api/v1/info")
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_api_key"


def test_info_with_valid_key(client, valid_key):
    resp = client.get("/api/v1/info", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    assert "version" in resp.json()


def test_agents_api_lists_registered_profiles(client, valid_key, monkeypatch, tmp_path: Path):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                    "cost_model": "cheap-default",
                    "stability_model": "stable-default",
                    "task_type_model_overrides": {"message.send": "message-default"},
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                        "enable_planning": False,
                        "routing_keywords": ["finance", "bank campaign"],
                    }
                },
            }
        }
    )
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["agents"][1]["agent_id"] == "finance_writer"
    assert body["agents"][1]["model"] == "deepseek-chat"
    assert body["agents"][1]["routing_keywords"] == ["finance", "bank campaign"]
    assert body["actions_total"] == 0
    assert body["pending_reload"] is False
    assert body["pending_reload_count"] == 0

    resp_disable = client.post(
        "/api/v1/agents/finance_writer/planning/disable",
        headers={"X-API-Key": valid_key},
    )
    assert resp_disable.status_code == 200
    assert resp_disable.json()["planning_enabled"] is False
    assert resp_disable.json()["reload_required"] is True
    assert cfg.agents.profiles["finance_writer"].enable_planning is False

    action_rows = (tmp_path / "dashboard" / "agent_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 1
    assert '"action": "planning_disable"' in action_rows[0]

    resp_default = client.post(
        "/api/v1/agents/default/planning/disable",
        headers={"X-API-Key": valid_key},
    )
    assert resp_default.status_code == 200
    assert resp_default.json()["agent_id"] == "default"
    assert cfg.agents.defaults.enable_planning is False

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["actions_total"] == 2
    assert body["recent_actions"][0]["agent_id"] == "default"

    resp_model = client.post(
        "/api/v1/agents/finance_writer/model",
        headers={"X-API-Key": valid_key},
        json={"model": "gpt-4.1-mini"},
    )
    assert resp_model.status_code == 200
    assert resp_model.json()["model"] == "gpt-4.1-mini"
    assert resp_model.json()["before_model"] == "deepseek-chat"
    assert resp_model.json()["reload_required"] is True
    assert cfg.agents.profiles["finance_writer"].model == "gpt-4.1-mini"

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["actions_total"] == 3
    assert body["recent_actions"][0]["action"] == "model_update"
    assert body["recent_actions"][0]["detail"] == "deepseek-chat -> gpt-4.1-mini"

    resp_model_policy = client.post(
        "/api/v1/agents/finance_writer/model-policy",
        headers={"X-API-Key": valid_key},
        json={
            "vision_model": "gpt-4.1-mini",
            "thinking_model": "o3-mini",
            "fallback_model": "gpt-4.1-mini",
            "cost_model": "gpt-4.1-nano",
            "stability_model": "gpt-4.1",
            "intent_model_overrides": {"finance": "o3-mini", "wealth": "gpt-4.1-mini"},
            "task_type_model_overrides": {"message.send": "gpt-4.1-nano"},
            "allowed_models": ["gpt-4.1-mini", "o3-mini"],
        },
    )
    assert resp_model_policy.status_code == 200
    assert resp_model_policy.json()["policy"]["vision_model"] == "gpt-4.1-mini"
    assert resp_model_policy.json()["policy"]["thinking_model"] == "o3-mini"
    assert resp_model_policy.json()["policy"]["fallback_model"] == "gpt-4.1-mini"
    assert resp_model_policy.json()["policy"]["cost_model"] == "gpt-4.1-nano"
    assert resp_model_policy.json()["policy"]["stability_model"] == "gpt-4.1"
    assert resp_model_policy.json()["policy"]["intent_model_overrides"] == {
        "finance": "o3-mini",
        "wealth": "gpt-4.1-mini",
    }
    assert resp_model_policy.json()["policy"]["task_type_model_overrides"] == {
        "message.send": "gpt-4.1-nano"
    }
    assert resp_model_policy.json()["policy"]["allowed_models"] == ["gpt-4.1-mini", "o3-mini"]
    assert resp_model_policy.json()["before_policy"]["vision_model"] == ""
    assert resp_model_policy.json()["before_policy"]["cost_model"] == "cheap-default"
    assert resp_model_policy.json()["before_policy"]["stability_model"] == "stable-default"
    assert resp_model_policy.json()["reload_required"] is True
    assert cfg.agents.profiles["finance_writer"].vision_model == "gpt-4.1-mini"
    assert cfg.agents.profiles["finance_writer"].thinking_model == "o3-mini"
    assert cfg.agents.profiles["finance_writer"].fallback_model == "gpt-4.1-mini"
    assert cfg.agents.profiles["finance_writer"].cost_model == "gpt-4.1-nano"
    assert cfg.agents.profiles["finance_writer"].stability_model == "gpt-4.1"
    assert cfg.agents.profiles["finance_writer"].intent_model_overrides == {
        "finance": "o3-mini",
        "wealth": "gpt-4.1-mini",
    }
    assert cfg.agents.profiles["finance_writer"].task_type_model_overrides == {
        "message.send": "gpt-4.1-nano"
    }
    assert cfg.agents.profiles["finance_writer"].allowed_models == ["gpt-4.1-mini", "o3-mini"]

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["actions_total"] == 4
    assert body["recent_actions"][0]["action"] == "model_policy_update"
    assert "vision_model: - -> gpt-4.1-mini" in body["recent_actions"][0]["detail"]
    assert "cost_model: cheap-default -> gpt-4.1-nano" in body["recent_actions"][0]["detail"]

    resp_routing = client.post(
        "/api/v1/agents/finance_writer/routing-keywords",
        headers={"X-API-Key": valid_key},
        json={"routing_keywords": ["wealth", "portfolio review"]},
    )
    assert resp_routing.status_code == 200
    assert resp_routing.json()["routing_keywords"] == ["wealth", "portfolio review"]
    assert resp_routing.json()["before_routing_keywords"] == ["finance", "bank campaign"]
    assert resp_routing.json()["reload_required"] is True
    assert cfg.agents.profiles["finance_writer"].routing_keywords == ["wealth", "portfolio review"]

    resp_default_routing = client.post(
        "/api/v1/agents/default/routing-keywords",
        headers={"X-API-Key": valid_key},
        json={"routing_keywords": ["anything"]},
    )
    assert resp_default_routing.status_code == 400

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["actions_total"] == 5
    assert body["recent_actions"][0]["action"] == "routing_keywords_update"
    assert body["recent_actions"][0]["detail"] == "finance, bank campaign -> wealth, portfolio review"
    assert body["pending_reload"] is True
    assert body["pending_reload_count"] == 5

    resp_history = client.get(
        "/api/v1/agents/history",
        headers={"X-API-Key": valid_key},
        params={"agent_id": "finance_writer", "action": "model_update", "actor": "api_key"},
    )
    assert resp_history.status_code == 200
    history_body = resp_history.json()
    assert history_body["total"] == 1
    assert history_body["returned"] == 1
    assert history_body["rows"][0]["agent_id"] == "finance_writer"
    assert history_body["rows"][0]["action"] == "model_update"
    assert history_body["rows"][0]["detail"] == "deepseek-chat -> gpt-4.1-mini"

    resp_history_page = client.get(
        "/api/v1/agents/history",
        headers={"X-API-Key": valid_key},
        params={"limit": 2, "offset": 1},
    )
    assert resp_history_page.status_code == 200
    assert resp_history_page.json()["total"] == 5
    assert resp_history_page.json()["returned"] == 2
    assert resp_history_page.json()["offset"] == 1

    resp_policy_history = client.get(
        "/api/v1/agents/history",
        headers={"X-API-Key": valid_key},
        params={"action": "model_policy_update"},
    )
    assert resp_policy_history.status_code == 200
    assert resp_policy_history.json()["total"] == 1
    assert resp_policy_history.json()["rows"][0]["action"] == "model_policy_update"

    resp_history_csv = client.get(
        "/api/v1/agents/history",
        headers={"X-API-Key": valid_key},
        params={"action": "routing_keywords_update", "format": "csv"},
    )
    assert resp_history_csv.status_code == 200
    assert resp_history_csv.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=agent-action-history.csv" == resp_history_csv.headers[
        "content-disposition"
    ]
    csv_text = resp_history_csv.text
    assert "agent_id,action,planning_enabled,actor,detail,trace_id" in csv_text
    assert "finance_writer,routing_keywords_update" in csv_text

    resp_apply = client.post("/api/v1/agents/reload-ack", headers={"X-API-Key": valid_key})
    assert resp_apply.status_code == 200
    assert resp_apply.json()["applied"] is True
    assert resp_apply.json()["pending_before"] == 5
    assert resp_apply.json()["pending_after"] == 0
    assert resp_apply.json()["cleared_count"] == 5

    apply_rows = (tmp_path / "dashboard" / "agent_apply.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(apply_rows) == 1
    assert '"event": "agent.config.applied"' in apply_rows[0]

    resp = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending_reload"] is False
    assert body["pending_reload_count"] == 0


def test_agents_route_preview_api_resolves_bound_keyword_and_channel_default(
    client, valid_key, monkeypatch, tmp_path: Path
):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {"workspace": str(tmp_path / "default-ws"), "model": "default-model"},
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                        "routing_keywords": ["finance", "bank campaign"],
                    },
                    "realty_writer": {
                        "display_name": "Realty Writer",
                        "model": "gpt-4.1-mini",
                        "routing_keywords": ["property launch"],
                    },
                },
            },
            "channels": {"webchat": {"agent_profile": "realty_writer"}},
        }
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    route_store = AgentRouteStore(tmp_path / "channels" / "agent_routes.db")
    route_store.set_route(
        channel="webchat",
        chat_id="chat-1",
        user_id="user-1",
        agent_id="finance_writer",
        reason="manual_bind",
        at_ms=100,
    )

    bound_resp = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "content": "hello there",
        },
    )
    assert bound_resp.status_code == 200
    assert bound_resp.json()["decision"]["agent_id"] == "finance_writer"
    assert bound_resp.json()["decision"]["reason"] == "bound_route"
    assert bound_resp.json()["bound_agent_id"] == "finance_writer"
    assert bound_resp.json()["route_audit_total"] == 1

    keyword_resp = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-2",
            "user_id": "user-2",
            "content": "need a bank campaign draft",
        },
    )
    assert keyword_resp.status_code == 200
    assert keyword_resp.json()["decision"]["agent_id"] == "finance_writer"
    assert keyword_resp.json()["decision"]["reason"] == "keyword"
    assert keyword_resp.json()["decision"]["matched_keyword"] == "bank campaign"
    assert keyword_resp.json()["channel_profile"] == "realty_writer"

    channel_resp = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-3",
            "user_id": "user-3",
            "content": "hello there",
        },
    )
    assert channel_resp.status_code == 200
    assert channel_resp.json()["decision"]["agent_id"] == "realty_writer"
    assert channel_resp.json()["decision"]["reason"] == "channel_profile"

    explicit_resp = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-4",
            "user_id": "user-4",
            "content": "property launch plan",
            "explicit_agent_id": "alpha",
        },
    )
    assert explicit_resp.status_code == 200
    assert explicit_resp.json()["decision"]["agent_id"] == "alpha"
    assert explicit_resp.json()["decision"]["reason"] == "explicit"
    assert explicit_resp.json()["decision"]["registered"] is False

    bind_resp = client.post(
        "/api/v1/agents/route-bind",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-2",
            "user_id": "user-2",
            "agent_id": "realty_writer",
        },
    )
    assert bind_resp.status_code == 200
    assert bind_resp.json()["bound_agent_id"] == "realty_writer"
    assert bind_resp.json()["route_audit_total"] == 1

    rebound_resp = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-2",
            "user_id": "user-2",
            "content": "need a bank campaign draft",
        },
    )
    assert rebound_resp.status_code == 200
    assert rebound_resp.json()["decision"]["agent_id"] == "realty_writer"
    assert rebound_resp.json()["decision"]["reason"] == "bound_route"
    assert rebound_resp.json()["route_audit_total"] == 1

    clear_resp = client.post(
        "/api/v1/agents/route-clear",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-2",
            "user_id": "user-2",
        },
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["cleared"] is True
    assert clear_resp.json()["previous_agent_id"] == "realty_writer"
    assert clear_resp.json()["bound_agent_id"] == ""
    assert clear_resp.json()["route_audit_total"] == 2

    cleared_preview = client.post(
        "/api/v1/agents/route-preview",
        headers={"X-API-Key": valid_key},
        json={
            "channel": "webchat",
            "chat_id": "chat-2",
            "user_id": "user-2",
            "content": "need a bank campaign draft",
        },
    )
    assert cleared_preview.status_code == 200
    assert cleared_preview.json()["decision"]["agent_id"] == "finance_writer"
    assert cleared_preview.json()["decision"]["reason"] == "keyword"
    assert cleared_preview.json()["bound_agent_id"] == ""
    assert cleared_preview.json()["route_audit_total"] == 2


def test_agent_detail_api_returns_effective_profile_and_channel_references(
    client, valid_key, monkeypatch, tmp_path: Path
):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                    "enable_planning": True,
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "description": "finance specialist",
                        "workspace": str(tmp_path / "finance-ws"),
                        "model": "deepseek-chat",
                        "skill_names": ["content_gen"],
                        "system_prompt": "You are the finance agent.",
                        "allowed_tools": ["rag_retrieve", "content_gen"],
                        "denied_tools": ["exec"],
                        "allowed_models": ["deepseek-chat", "gpt-4.1-mini"],
                        "cost_model": "gpt-4.1-nano",
                        "stability_model": "gpt-4.1",
                        "intent_model_overrides": {"finance": "gpt-4.1-mini"},
                        "task_type_model_overrides": {"message.send": "gpt-4.1-nano"},
                        "routing_keywords": ["finance"],
                    }
                },
            },
            "channels": {"webchat": {"agent_profile": "finance_writer"}},
        }
    )
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)

    resp = client.get("/api/v1/agents/finance_writer", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "finance_writer"
    assert body["display_name"] == "Finance Writer"
    assert body["effective"]["workspace"] == str((tmp_path / "finance-ws").resolve())
    assert body["effective"]["model"] == "deepseek-chat"
    assert body["effective"]["system_prompt"] == "You are the finance agent."
    assert body["effective"]["skill_names"] == ["content_gen"]
    assert body["effective"]["allowed_tools"] == ["rag_retrieve", "content_gen"]
    assert body["effective"]["denied_tools"] == ["exec"]
    assert body["effective"]["allowed_models"] == ["deepseek-chat", "gpt-4.1-mini"]
    assert body["effective"]["cost_model"] == "gpt-4.1-nano"
    assert body["effective"]["stability_model"] == "gpt-4.1"
    assert body["effective"]["intent_model_overrides"] == {"finance": "gpt-4.1-mini"}
    assert body["effective"]["task_type_model_overrides"] == {"message.send": "gpt-4.1-nano"}
    assert body["profile_overrides"]["routing_keywords"] == ["finance"]
    assert body["profile_overrides"]["skill_names"] == ["content_gen"]
    assert body["channel_references"] == [{"channel": "webchat", "display_name": "WebChat"}]

    default_resp = client.get("/api/v1/agents/default", headers={"X-API-Key": valid_key})
    assert default_resp.status_code == 200
    assert default_resp.json()["profile_overrides"] == {}


def test_agent_profile_save_and_delete_api(client, valid_key, monkeypatch, tmp_path: Path):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                    "enable_planning": True,
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                        "routing_keywords": ["finance"],
                    }
                },
            },
            "channels": {"webchat": {"agent_profile": "finance_writer"}},
        }
    )
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    create_resp = client.post(
        "/api/v1/agents/realty_writer/profile",
        headers={"X-API-Key": valid_key},
        json={
            "profile": {
                "display_name": "Realty Writer",
                "description": "real estate specialist",
                "model": "gpt-4.1-mini",
                "workspace": str(tmp_path / "realty-ws"),
                "routing_keywords": ["property launch"],
                "skill_names": ["content_gen"],
                "enable_planning": False,
            }
        },
    )
    assert create_resp.status_code == 200
    create_body = create_resp.json()
    assert create_body["created"] is True
    assert create_body["profile"]["display_name"] == "Realty Writer"
    assert create_body["profile"]["skill_names"] == ["content_gen"]
    assert cfg.agents.profiles["realty_writer"].model == "gpt-4.1-mini"

    update_resp = client.post(
        "/api/v1/agents/finance_writer/profile",
        headers={"X-API-Key": valid_key},
        json={
            "profile": {
                "display_name": "Finance Writer v2",
                "description": "updated finance specialist",
                "model": "gpt-4.1-mini",
                "workspace": str(tmp_path / "finance-ws"),
                "routing_keywords": ["wealth"],
                "skill_names": ["content_gen"],
                "allowed_tools": ["content_gen"],
                "denied_tools": ["exec"],
                "enable_planning": False,
            }
        },
    )
    assert update_resp.status_code == 200
    update_body = update_resp.json()
    assert update_body["created"] is False
    assert "display_name" in update_body["changed_fields"]
    assert update_body["profile"]["routing_keywords"] == ["wealth"]
    assert cfg.agents.profiles["finance_writer"].display_name == "Finance Writer v2"

    delete_resp = client.delete(
        "/api/v1/agents/finance_writer",
        headers={"X-API-Key": valid_key},
    )
    assert delete_resp.status_code == 200
    delete_body = delete_resp.json()
    assert delete_body["deleted"] is True
    assert delete_body["channel_references"] == [{"channel": "webchat", "display_name": "WebChat"}]
    assert "finance_writer" not in cfg.agents.profiles

    history_resp = client.get(
        "/api/v1/agents/history",
        headers={"X-API-Key": valid_key},
        params={"agent_id": "finance_writer"},
    )
    assert history_resp.status_code == 200
    actions = [row["action"] for row in history_resp.json()["rows"]]
    assert "profile_update" in actions
    assert "profile_delete" in actions

    default_delete = client.delete("/api/v1/agents/default", headers={"X-API-Key": valid_key})
    assert default_delete.status_code == 400


def test_model_routing_api_supports_filters(client, valid_key, monkeypatch, tmp_path: Path):
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    (tmp_path / "dashboard").mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    (tmp_path / "dashboard" / "model_routing.log.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "at_ms": now_ms - (5 * 3600 * 1000),
                        "trace_id": "trace-model-1",
                        "channel": "cli",
                        "intent_name": "weather",
                        "selected_model": "weather-model",
                        "reason": "intent_override:weather",
                    }
                ),
                json.dumps(
                    {
                        "at_ms": now_ms - (30 * 60 * 1000),
                        "trace_id": "trace-model-2",
                        "channel": "webchat",
                        "intent_name": "finance",
                        "selected_model": "default-model",
                        "reason": "default",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    resp = client.get(
        "/api/v1/model-routing?model=weather-model&reason=intent_override:weather",
        headers={"X-API-Key": valid_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["filters"]["model"] == "weather-model"
    assert body["events"][0]["trace_id"] == "trace-model-1"
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["returned"] == 1

    paged = client.get(
        "/api/v1/model-routing?limit=1&offset=1",
        headers={"X-API-Key": valid_key},
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert paged.json()["returned"] == 1
    assert paged.json()["events"][0]["trace_id"] == "trace-model-1"

    csv_resp = client.get(
        "/api/v1/model-routing?format=csv&limit=1",
        headers={"X-API-Key": valid_key},
    )
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=model-routing-events.csv" in csv_resp.headers["content-disposition"]
    assert "selected_model" in csv_resp.text
    assert "trace-model-2" in csv_resp.text

    since_resp = client.get(
        "/api/v1/model-routing?since_hours=1",
        headers={"X-API-Key": valid_key},
    )
    assert since_resp.status_code == 200
    assert since_resp.json()["total"] == 1
    assert since_resp.json()["events"][0]["trace_id"] == "trace-model-2"
    assert since_resp.json()["filters"]["since_hours"] == 1

    from_resp = client.get(
        f"/api/v1/model-routing?from_at_ms={now_ms - (60 * 60 * 1000)}&to_at_ms={now_ms}",
        headers={"X-API-Key": valid_key},
    )
    assert from_resp.status_code == 200
    assert from_resp.json()["total"] == 1
    assert from_resp.json()["events"][0]["trace_id"] == "trace-model-2"
    assert from_resp.json()["summary"]["bucket_minutes"] == 60
    assert from_resp.json()["summary"]["channels"][0]["channel"] == "webchat"
    assert from_resp.json()["summary"]["buckets"][0]["count"] == 1

    bucket_resp = client.get(
        "/api/v1/model-routing?bucket_minutes=30",
        headers={"X-API-Key": valid_key},
    )
    assert bucket_resp.status_code == 200
    bucket_body = bucket_resp.json()
    assert bucket_body["summary"]["bucket_minutes"] == 30
    assert {row["model"] for row in bucket_body["summary"]["models"]} == {
        "weather-model",
        "default-model",
    }
    assert {row["reason"] for row in bucket_body["summary"]["reasons"]} == {
        "intent_override:weather",
        "default",
    }
    assert bucket_body["filters"]["bucket_minutes"] == 30


def test_invoke_no_key_returns_401(client):
    resp = client.post("/api/v1/agent/invoke", json={"message": "hello"})
    assert resp.status_code == 401


def test_invoke_with_key(client, valid_key):
    resp = client.post(
        "/api/v1/agent/invoke", json={"message": "test"}, headers={"X-API-Key": valid_key}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "response" in body
    assert "session_id" in body


def test_env_var_api_key(monkeypatch, client):
    raw = "nc-test-env-key-abc123"
    monkeypatch.setenv("zen_claw_API_KEYS", raw)
    resp = client.get("/api/v1/info", headers={"X-API-Key": raw})
    assert resp.status_code != 401


def test_sse_stream_content_type(client, valid_key):
    with client.stream(
        "POST",
        "/api/v1/agent/invoke/stream",
        headers={"X-API-Key": valid_key},
        json={"message": "stream test"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        chunk = next(resp.iter_text())
        assert "data:" in chunk


def test_skills_inventory_and_toggle_api(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    skill_dir = cfg.workspace_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "tests").mkdir(parents=True, exist_ok=True)
    (skill_dir / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted","runtime_contract":{"intent":"assist","intent_mode":"skill_first"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)

    resp_list = client.get("/api/v1/skills", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert resp_list.json()["skills"][0]["name"] == "alpha"
    assert resp_list.json()["tested_count"] == 1
    assert resp_list.json()["trusted_count"] == 1
    assert resp_list.json()["runtime_mode_breakdown"]["skill_first"] == 1
    assert resp_list.json()["checks_total"] == 0
    assert resp_list.json()["exports_total"] == 0
    assert resp_list.json()["skills"][0]["intake_status"] == "local"
    assert resp_list.json()["skills"][0]["promote_status"] == "candidate"
    assert resp_list.json()["skills"][0]["intake_summary"]["verdict"] == "accept"

    incoming_dir = tmp_path / "incoming" / "beta"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    (incoming_dir / "SKILL.md").write_text("# beta\n", encoding="utf-8")
    (incoming_dir / "manifest.json").write_text(
        '{"name":"beta","version":"1.0.0","description":"beta","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted","runtime_contract":{"intent":"assist","intent_mode":"skill_first"}}',
        encoding="utf-8",
    )

    resp_install = client.post(
        "/api/v1/skills/install",
        headers={"X-API-Key": valid_key},
        json={
            "source": str(incoming_dir),
            "strict_manifest": True,
            "source_url": "https://example.com/beta.zip",
            "source_version": "1.0.0",
            "last_sync_checked_at": "2026-03-28T15:30:00Z",
            "intake_status": "intake_review",
        },
    )
    assert resp_install.status_code == 200
    installed = resp_install.json()
    assert installed["ok"] is True
    assert installed["name"] == "beta"
    assert installed["source_kind"] == "dir"
    assert installed["governance"]["source_url"] == "https://example.com/beta.zip"
    assert (cfg.workspace_path / "skills" / "beta" / "SKILL.md").exists()

    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 1
    assert '"action": "install"' in action_rows[0]

    resp_disable = client.post("/api/v1/skills/alpha/disable", headers={"X-API-Key": valid_key})
    assert resp_disable.status_code == 200
    assert resp_disable.json()["enabled"] is False
    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 2
    assert '"action": "disable"' in action_rows[1]

    resp_enable = client.post("/api/v1/skills/alpha/enable", headers={"X-API-Key": valid_key})
    assert resp_enable.status_code == 200
    assert resp_enable.json()["enabled"] is True
    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 3
    assert '"action": "enable"' in action_rows[2]

    resp_batch_disable = client.post(
        "/api/v1/skills/disable-batch",
        headers={"X-API-Key": valid_key},
        json={"names": ["alpha", "beta", "missing"]},
    )
    assert resp_batch_disable.status_code == 200
    batch_disable = resp_batch_disable.json()
    assert batch_disable["enabled"] is False
    assert batch_disable["changed_count"] == 2
    assert batch_disable["missing_names"] == ["missing"]
    assert {row["name"] for row in batch_disable["rows"]} == {"alpha", "beta"}

    resp_batch_enable = client.post(
        "/api/v1/skills/enable-batch",
        headers={"X-API-Key": valid_key},
        json={"names": ["alpha", "beta"]},
    )
    assert resp_batch_enable.status_code == 200
    batch_enable = resp_batch_enable.json()
    assert batch_enable["enabled"] is True
    assert batch_enable["changed_count"] == 2

    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 7
    assert '"detail": "batch disable"' in action_rows[3]
    assert '"detail": "batch disable"' in action_rows[4]
    assert '"detail": "batch enable"' in action_rows[5]
    assert '"detail": "batch enable"' in action_rows[6]

    resp_preflight = client.post("/api/v1/skills/alpha/preflight", headers={"X-API-Key": valid_key})
    assert resp_preflight.status_code == 200
    preflight = resp_preflight.json()
    assert preflight["name"] == "alpha"
    assert preflight["ok"] is True
    assert preflight["manifest_ok"] is True
    assert preflight["integrity_ok"] is True
    assert preflight["tests_present"] is True
    assert preflight["detail"] == "manifest/integrity checks passed"

    check_rows = (tmp_path / "dashboard" / "skills_checks.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(check_rows) == 1
    assert '"event": "skills.preflight"' in check_rows[0]

    resp_batch_preflight = client.post(
        "/api/v1/skills/preflight-batch",
        headers={"X-API-Key": valid_key},
        json={"names": ["alpha", "beta", "missing"], "include_disabled": True},
    )
    assert resp_batch_preflight.status_code == 200
    batch = resp_batch_preflight.json()
    assert batch["checked_count"] == 2
    assert batch["ok_count"] == 2
    assert batch["issue_count"] == 0
    assert batch["missing_names"] == ["missing"]
    assert {row["name"] for row in batch["rows"]} == {"alpha", "beta"}

    resp_list = client.get("/api/v1/skills", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert resp_list.json()["checks_total"] == 3
    assert resp_list.json()["failed_checks"] == 0
    assert resp_list.json()["recent_checks"][0]["skill_name"] in {"alpha", "beta"}
    assert resp_list.json()["recent_checks"][0]["detail"] == "manifest/integrity checks passed"
    assert resp_list.json()["skills"][0]["last_check"]["ok"] is True

    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["name"] == "alpha"
    assert detail["enabled"] is True
    assert detail["preflight"]["manifest_ok"] is True
    assert detail["preflight"]["integrity_ok"] is True
    assert detail["preflight"]["tests_present"] is True
    assert detail["manifest"]["name"] == "alpha"
    assert detail["recent_checks"][0]["skill_name"] == "alpha"
    assert detail["package_policy"]["retention_hours"] == 24
    assert detail["package_policy"]["require_export_for_restore"] is True
    assert detail["package_policy"]["restore_ready"] is False
    assert detail["governance"]["intake_status"] == "local"
    assert detail["governance"]["promote_status"] == "candidate"
    resp_restore_plan = client.get("/api/v1/skills/alpha/restore-plan", headers={"X-API-Key": valid_key})
    assert resp_restore_plan.status_code == 200
    assert resp_restore_plan.json()["restore_plan"]["ready"] is False
    assert "no exported package is available" in resp_restore_plan.json()["restore_plan"]["blocked_reasons"]

    resp_installed_detail = client.get("/api/v1/skills/beta", headers={"X-API-Key": valid_key})
    assert resp_installed_detail.status_code == 200
    installed_detail = resp_installed_detail.json()
    assert installed_detail["name"] == "beta"
    assert installed_detail["governance"]["source_url"] == "https://example.com/beta.zip"
    assert installed_detail["governance"]["source_version"] == "1.0.0"
    assert installed_detail["governance"]["last_sync_checked_at"] == "2026-03-28T15:30:00Z"
    assert installed_detail["governance"]["intake_status"] == "intake_review"
    assert installed_detail["recent_actions"][0]["action"] in {"install", "enable", "disable"}
    assert any(
        row["action"] == "install" and "installed skill: beta" in row["detail"]
        for row in installed_detail["recent_actions"]
    )
    assert installed_detail["package_state"]["selected_physical_dir"] == "beta"
    assert installed_detail["package_state"]["mapped_directories"] == ["beta"]
    assert installed_detail["package_state"]["journal_pending_count"] == 0

    resp_export = client.post("/api/v1/skills/alpha/export", headers={"X-API-Key": valid_key})
    assert resp_export.status_code == 200
    exported = resp_export.json()
    assert exported["ok"] is True
    assert exported["skill_name"] == "alpha"
    assert exported["out_path"].endswith("alpha.zip")
    assert exported["exported_physical_dir"] == "alpha"
    assert exported["exported_version"] == "1.0.0"
    assert Path(exported["out_path"]).exists()

    export_rows = (tmp_path / "dashboard" / "skills_exports.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(export_rows) == 1
    assert '"event": "skills.export"' in export_rows[0]

    resp_list = client.get("/api/v1/skills", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert resp_list.json()["exports_total"] == 1
    assert resp_list.json()["recent_exports"][0]["skill_name"] == "alpha"
    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    assert resp_detail.json()["recent_exports"][0]["skill_name"] == "alpha"
    assert resp_detail.json()["package_state"]["latest_export_path"].endswith("alpha.zip")
    assert resp_detail.json()["package_state"]["package_rows"][0]["physical_dir"] == "alpha"
    assert resp_detail.json()["package_policy"]["restore_ready"] is True
    resp_restore_plan = client.get("/api/v1/skills/alpha/restore-plan", headers={"X-API-Key": valid_key})
    assert resp_restore_plan.status_code == 200
    assert resp_restore_plan.json()["restore_plan"]["ready"] is True
    assert resp_restore_plan.json()["restore_plan"]["blocked_reasons"] == []

    resp_policy = client.post(
        "/api/v1/skills/alpha/package-policy",
        headers={"X-API-Key": valid_key},
        json={
            "preferred_physical_dir": "alpha",
            "preferred_version": "1.0.0",
            "retention_hours": 72,
            "require_export_for_restore": True,
        },
    )
    assert resp_policy.status_code == 200
    policy_body = resp_policy.json()
    assert policy_body["ok"] is True
    assert policy_body["package_policy"]["preferred_physical_dir"] == "alpha"
    assert policy_body["package_policy"]["preferred_version"] == "1.0.0"
    assert policy_body["package_policy"]["retention_hours"] == 72
    assert policy_body["package_policy"]["restore_ready"] is True

    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    assert resp_detail.json()["package_policy"]["preferred_physical_dir"] == "alpha"
    assert resp_detail.json()["package_policy"]["preferred_version"] == "1.0.0"
    assert resp_detail.json()["package_policy"]["retention_hours"] == 72
    resp_restore_plan = client.get("/api/v1/skills/alpha/restore-plan", headers={"X-API-Key": valid_key})
    assert resp_restore_plan.status_code == 200
    assert resp_restore_plan.json()["restore_plan"]["preferred_physical_dir"] == "alpha"
    assert resp_restore_plan.json()["restore_plan"]["preferred_version"] == "1.0.0"

    resp_history = client.get(
        "/api/v1/skills/history",
        headers={"X-API-Key": valid_key},
        params={"skill_name": "alpha", "ok_only": "true"},
    )
    assert resp_history.status_code == 200
    history_body = resp_history.json()
    assert history_body["total"] == 3
    assert history_body["returned"] == 3
    assert {row["event_kind"] for row in history_body["rows"]} == {"check", "export"}
    assert history_body["rows"][0]["skill_name"] == "alpha"

    resp_history_page = client.get(
        "/api/v1/skills/history",
        headers={"X-API-Key": valid_key},
        params={"limit": 1, "offset": 1},
    )
    assert resp_history_page.status_code == 200
    assert resp_history_page.json()["returned"] == 1
    assert resp_history_page.json()["offset"] == 1

    resp_history_csv = client.get(
        "/api/v1/skills/history",
        headers={"X-API-Key": valid_key},
        params={"skill_name": "alpha", "format": "csv"},
    )
    assert resp_history_csv.status_code == 200
    assert resp_history_csv.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=skill-activity-history.csv" == resp_history_csv.headers[
        "content-disposition"
    ]
    csv_text = resp_history_csv.text
    assert "skill_name,event_kind,action,ok,actor,detail,trace_id" in csv_text
    assert "alpha,export,export,True" in csv_text

    resp_uninstall_alpha = client.post(
        "/api/v1/skills/alpha/uninstall", headers={"X-API-Key": valid_key}
    )
    assert resp_uninstall_alpha.status_code == 200
    assert (cfg.workspace_path / "skills" / "alpha").exists() is False

    resp_missing_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_missing_detail.status_code == 404

    resp_restore = client.post(
        "/api/v1/skills/alpha/restore", headers={"X-API-Key": valid_key}
    )
    assert resp_restore.status_code == 200
    restored = resp_restore.json()
    assert restored["ok"] is True
    assert restored["name"] == "alpha"
    assert restored["source"].endswith("alpha.zip")
    assert restored["restore_plan"]["source"] == "latest_export"
    assert restored["restore_plan"]["ready"] is True
    assert restored["package_policy"]["preferred_physical_dir"] == "alpha"
    assert (cfg.workspace_path / "skills" / "alpha" / "SKILL.md").exists()

    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    assert resp_detail.json()["recent_actions"][0]["action"] == "restore"
    assert "source=" in resp_detail.json()["recent_actions"][0]["detail"]

    resp_uninstall_alpha_again = client.post(
        "/api/v1/skills/alpha/uninstall", headers={"X-API-Key": valid_key}
    )
    assert resp_uninstall_alpha_again.status_code == 200

    resp_restore = client.post(
        "/api/v1/skills/alpha/restore-export", headers={"X-API-Key": valid_key}
    )
    assert resp_restore.status_code == 200
    restored = resp_restore.json()
    assert restored["ok"] is True
    assert restored["name"] == "alpha"
    assert restored["source"].endswith("alpha.zip")
    assert (cfg.workspace_path / "skills" / "alpha" / "SKILL.md").exists()

    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    assert resp_detail.json()["recent_actions"][0]["action"] == "restore_export"
    assert "source=" in resp_detail.json()["recent_actions"][0]["detail"]
    assert any(
        row["action"] == "package_policy" for row in resp_detail.json()["recent_actions"]
    )

    resp_promote = client.post(
        "/api/v1/skills/alpha/promote",
        headers={"X-API-Key": valid_key},
        json={
            "declarative_intent": "alpha_lookup",
            "note": "ready for manual Gate 1 registration",
        },
    )
    assert resp_promote.status_code == 200
    assert resp_promote.json()["declarative_intent"] == "alpha_lookup"

    resp_detail = client.get("/api/v1/skills/alpha", headers={"X-API-Key": valid_key})
    assert resp_detail.status_code == 200
    assert resp_detail.json()["governance"]["promote_status"] == "promoted"
    assert resp_detail.json()["governance"]["promote_target_intent"] == "alpha_lookup"
    assert any(
        row["action"] == "promote" for row in resp_detail.json()["recent_actions"]
    )

    resp_uninstall = client.post("/api/v1/skills/beta/uninstall", headers={"X-API-Key": valid_key})
    assert resp_uninstall.status_code == 200
    assert resp_uninstall.json()["name"] == "beta"
    assert (cfg.workspace_path / "skills" / "beta").exists() is False

    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 14
    assert '"action": "uninstall"' in action_rows[-1]

    resp_list = client.get("/api/v1/skills", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert {row["name"] for row in resp_list.json()["skills"]} == {"alpha"}
    assert resp_list.json()["journal_pending_count"] == 0
    assert resp_list.json()["versioned_dirs_count"] == 0


def test_skills_gc_preview_and_run_api(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    active_skill = cfg.workspace_path / "skills" / "alpha"
    active_skill.mkdir(parents=True, exist_ok=True)
    (active_skill / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (active_skill / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted","runtime_contract":{"intent":"assist","intent_mode":"skill_first"}}',
        encoding="utf-8",
    )
    stale_dir = cfg.workspace_path / "skills" / "alpha_v0_9_0"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "SKILL.md").write_text("# alpha legacy\n", encoding="utf-8")
    old_ts = time.time() - (72 * 3600)
    os.utime(stale_dir, (old_ts, old_ts))

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)

    resp_preview = client.post(
        "/api/v1/skills/gc-preview",
        headers={"X-API-Key": valid_key},
        json={"retention_hours": 24},
    )
    assert resp_preview.status_code == 200
    preview = resp_preview.json()
    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["physical_dir"] == "alpha_v0_9_0"

    resp_run = client.post(
        "/api/v1/skills/gc-run",
        headers={"X-API-Key": valid_key},
        json={"retention_hours": 24},
    )
    assert resp_run.status_code == 200
    body = resp_run.json()
    assert body["deleted_count"] == 1
    assert body["preview"]["candidate_count"] == 1
    assert stale_dir.exists() is False

    action_rows = (tmp_path / "dashboard" / "skills_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 1
    assert '"action": "gc_cleanup"' in action_rows[0]


def test_skill_restore_plan_enforces_preferred_export_version(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    skill_dir = cfg.workspace_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted"}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)

    resp_export = client.post("/api/v1/skills/alpha/export", headers={"X-API-Key": valid_key})
    assert resp_export.status_code == 200

    (skill_dir / "manifest.json").write_text(
        '{"name":"alpha","version":"2.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted"}',
        encoding="utf-8",
    )

    resp_policy = client.post(
        "/api/v1/skills/alpha/package-policy",
        headers={"X-API-Key": valid_key},
        json={
            "preferred_version": "2.0.0",
            "require_export_for_restore": True,
        },
    )
    assert resp_policy.status_code == 200

    resp_plan = client.get("/api/v1/skills/alpha/restore-plan", headers={"X-API-Key": valid_key})
    assert resp_plan.status_code == 200
    body = resp_plan.json()
    assert body["restore_plan"]["export_version"] == "1.0.0"
    assert body["restore_plan"]["preferred_version"] == "2.0.0"
    assert body["restore_plan"]["ready"] is False
    assert (
        "export package version does not match preferred version: 2.0.0"
        in body["restore_plan"]["blocked_reasons"]
    )

    resp_restore = client.post("/api/v1/skills/alpha/restore", headers={"X-API-Key": valid_key})
    assert resp_restore.status_code == 409
    assert "export package version does not match preferred version: 2.0.0" in resp_restore.json()[
        "detail"
    ]


def test_skill_restore_plan_enforces_preferred_export_physical_dir(
    client, valid_key, tmp_path, monkeypatch
):
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path / "ws")
    cfg.workspace_path.mkdir(parents=True, exist_ok=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    workspace_skills = cfg.workspace_path / "skills"
    skill_v1 = workspace_skills / "alpha_v1_0_0"
    skill_v2 = workspace_skills / "alpha_v2_0_0"
    skill_v1.mkdir(parents=True, exist_ok=True)
    skill_v2.mkdir(parents=True, exist_ok=True)
    (skill_v1 / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_v2 / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill_v1 / "manifest.json").write_text(
        '{"name":"alpha","version":"1.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted"}',
        encoding="utf-8",
    )
    (skill_v2 / "manifest.json").write_text(
        '{"name":"alpha","version":"2.0.0","description":"alpha","permissions":["read_file"],"scopes":["filesystem"],"trust":"trusted"}',
        encoding="utf-8",
    )

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.agent.skills.BUILTIN_SKILLS_DIR", builtin)

    from zen_claw.agent.skills import SkillsLoader

    loader = SkillsLoader(cfg.workspace_path)
    loader._skill_mapping = {"alpha": ["alpha_v1_0_0", "alpha_v2_0_0"]}
    loader._save_skill_mapping()

    resp_export = client.post("/api/v1/skills/alpha/export", headers={"X-API-Key": valid_key})
    assert resp_export.status_code == 200
    assert resp_export.json()["exported_physical_dir"] == "alpha_v2_0_0"
    assert resp_export.json()["exported_version"] == "2.0.0"

    resp_policy = client.post(
        "/api/v1/skills/alpha/package-policy",
        headers={"X-API-Key": valid_key},
        json={
            "preferred_physical_dir": "alpha_v1_0_0",
            "require_export_for_restore": True,
        },
    )
    assert resp_policy.status_code == 200

    resp_plan = client.get("/api/v1/skills/alpha/restore-plan", headers={"X-API-Key": valid_key})
    assert resp_plan.status_code == 200
    body = resp_plan.json()
    assert body["restore_plan"]["export_physical_dir"] == "alpha_v2_0_0"
    assert body["restore_plan"]["preferred_physical_dir"] == "alpha_v1_0_0"
    assert body["restore_plan"]["ready"] is False
    assert "export package dir does not match preferred dir: alpha_v1_0_0" in body["restore_plan"][
        "blocked_reasons"
    ]

    resp_restore = client.post("/api/v1/skills/alpha/restore", headers={"X-API-Key": valid_key})
    assert resp_restore.status_code == 409
    assert "export package dir does not match preferred dir: alpha_v1_0_0" in resp_restore.json()[
        "detail"
    ]


def test_channels_inventory_and_toggle_api(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.channels.slack.enabled = True
    cfg_path = tmp_path / "config.json"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    resp_list = client.get("/api/v1/channels", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    rows = {row["name"]: row for row in resp_list.json()["channels"]}
    runtime_rows = {row["channel_name"]: row for row in resp_list.json()["runtime_controls"]}
    assert rows["slack"]["enabled"] is True
    assert runtime_rows["webchat"]["supported"] is True
    assert runtime_rows["webchat"]["apply_supported"] is True
    assert runtime_rows["slack"]["supported"] is False

    resp_disable = client.post("/api/v1/channels/slack/disable", headers={"X-API-Key": valid_key})
    assert resp_disable.status_code == 200
    assert resp_disable.json()["enabled"] is False
    assert resp_disable.json()["reload_required"] is True
    assert cfg.channels.slack.enabled is False

    action_rows = (tmp_path / "dashboard" / "channel_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 1
    assert '"action": "disable"' in action_rows[0]

    resp_enable = client.post("/api/v1/channels/slack/enable", headers={"X-API-Key": valid_key})
    assert resp_enable.status_code == 200
    assert resp_enable.json()["enabled"] is True
    assert cfg.channels.slack.enabled is True

    resp_list = client.get("/api/v1/channels", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert resp_list.json()["pending_reload"] is True
    assert resp_list.json()["pending_reload_count"] == 2

    resp_history = client.get(
        "/api/v1/channels/history",
        headers={"X-API-Key": valid_key},
        params={"channel_name": "slack", "action": "disable", "actor": "api_key"},
    )
    assert resp_history.status_code == 200
    history_body = resp_history.json()
    assert history_body["total"] == 1
    assert history_body["returned"] == 1
    assert history_body["rows"][0]["channel_name"] == "slack"
    assert history_body["rows"][0]["action"] == "disable"

    resp_history_page = client.get(
        "/api/v1/channels/history",
        headers={"X-API-Key": valid_key},
        params={"limit": 1, "offset": 1},
    )
    assert resp_history_page.status_code == 200
    assert resp_history_page.json()["total"] == 2
    assert resp_history_page.json()["returned"] == 1
    assert resp_history_page.json()["offset"] == 1

    resp_history_csv = client.get(
        "/api/v1/channels/history",
        headers={"X-API-Key": valid_key},
        params={"channel_name": "slack", "format": "csv"},
    )
    assert resp_history_csv.status_code == 200
    assert resp_history_csv.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=channel-action-history.csv" == resp_history_csv.headers[
        "content-disposition"
    ]
    csv_text = resp_history_csv.text
    assert "channel_name,action,enabled,actor,detail,trace_id" in csv_text
    assert "slack,disable" in csv_text

    resp_apply = client.post("/api/v1/channels/reload-ack", headers={"X-API-Key": valid_key})
    assert resp_apply.status_code == 200
    assert resp_apply.json()["applied"] is True
    assert resp_apply.json()["pending_before"] == 2
    assert resp_apply.json()["pending_after"] == 0
    assert resp_apply.json()["cleared_count"] == 2

    apply_rows = (tmp_path / "dashboard" / "channel_apply.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(apply_rows) == 1
    assert '"event": "channels.config.applied"' in apply_rows[0]

    resp_list = client.get("/api/v1/channels", headers={"X-API-Key": valid_key})
    assert resp_list.status_code == 200
    assert resp_list.json()["pending_reload"] is False
    assert resp_list.json()["pending_reload_count"] == 0


def test_channel_runtime_control_api(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    started = {"count": 0}
    stopped = {"count": 0}

    async def _fake_start():
        started["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "running",
            "running_instances": 1,
            "known_loops": 1,
        }

    async def _fake_stop():
        stopped["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "stopped",
            "running_instances": 0,
            "known_loops": 1,
            "stopped_instances": 1,
        }

    monkeypatch.setattr("zen_claw.dashboard.server._start_webchat_runtime", _fake_start)
    monkeypatch.setattr("zen_claw.dashboard.server._stop_webchat_runtime", _fake_stop)
    monkeypatch.setattr(
        "zen_claw.dashboard.server._webchat_runtime_summary",
        lambda: {
            "channel_name": "webchat",
            "supported": True,
            "state": "stopped",
            "running_instances": 0,
            "known_loops": 0,
        },
    )

    runtime_resp = client.get("/api/v1/channels/runtime", headers={"X-API-Key": valid_key})
    assert runtime_resp.status_code == 200
    runtime_rows = {row["channel_name"]: row for row in runtime_resp.json()["rows"]}
    assert runtime_rows["webchat"]["state"] == "stopped"
    assert runtime_rows["slack"]["supported"] is False

    start_resp = client.post("/api/v1/channels/webchat/runtime/start", headers={"X-API-Key": valid_key})
    assert start_resp.status_code == 200
    assert start_resp.json()["runtime"]["state"] == "running"
    assert started["count"] == 1

    stop_resp = client.post("/api/v1/channels/webchat/runtime/stop", headers={"X-API-Key": valid_key})
    assert stop_resp.status_code == 200
    assert stop_resp.json()["runtime"]["state"] == "stopped"
    assert stopped["count"] == 1

    action_rows = (tmp_path / "dashboard" / "channel_actions.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(action_rows) == 2
    assert '"action": "runtime_start"' in action_rows[0]
    assert '"action": "runtime_stop"' in action_rows[1]


def test_channel_reload_ack_restarts_webchat_runtime_when_pending(
    client, valid_key, tmp_path, monkeypatch
):
    cfg = Config()
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)

    started = {"count": 0}
    stopped = {"count": 0}

    async def _fake_start():
        started["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "running",
            "running_instances": 1,
            "known_loops": 1,
        }

    async def _fake_stop():
        stopped["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "stopped",
            "running_instances": 0,
            "known_loops": 1,
            "stopped_instances": 1,
        }

    monkeypatch.setattr("zen_claw.dashboard.server._start_webchat_runtime", _fake_start)
    monkeypatch.setattr("zen_claw.dashboard.server._stop_webchat_runtime", _fake_stop)
    monkeypatch.setattr(
        "zen_claw.dashboard.server._webchat_runtime_summary",
        lambda: {
            "channel_name": "webchat",
            "supported": True,
            "state": "running",
            "running_instances": 1,
            "known_loops": 1,
        },
    )

    disable_resp = client.post("/api/v1/channels/webchat/disable", headers={"X-API-Key": valid_key})
    assert disable_resp.status_code == 200

    resp_apply = client.post("/api/v1/channels/reload-ack", headers={"X-API-Key": valid_key})
    assert resp_apply.status_code == 200
    body = resp_apply.json()
    assert body["applied"] is True
    assert body["runtime_execution"]["executed"] is True
    assert body["runtime_execution"]["status"] == "restarted"
    assert body["runtime_execution"]["after_state"] == "running"
    assert started["count"] == 1
    assert stopped["count"] == 1


def test_crawler_sources_and_run_api(client, valid_key, tmp_path, monkeypatch):
    from zen_claw.skills.crawler.extractor import CrawlerSource
    from zen_claw.skills.crawler.scheduler import upsert_source

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    upsert_source(
        tmp_path / "crawler" / "sources.json",
        CrawlerSource(
            name="news",
            url="https://example.com/news",
            notebook_id="crawler_kb",
            use_browser=True,
            selector=".body",
        ),
    )

    async def _fake_crawl_to_rag(self, source):
        assert source.name == "news"
        return {
            "crawl_name": source.name,
            "crawl_mode": "browser",
            "change_status": "updated",
            "documents": 1,
            "chunks_added": 4,
            "notebook": source.notebook_id,
        }

    monkeypatch.setattr(
        "zen_claw.skills.crawler.extractor.CrawlerExtractor.crawl_to_rag",
        _fake_crawl_to_rag,
    )

    read_resp = client.get("/api/v1/crawler/sources", headers={"X-API-Key": valid_key})
    assert read_resp.status_code == 200
    assert read_resp.json()["total"] == 1
    assert read_resp.json()["sources"][0]["name"] == "news"

    save_resp = client.post(
        "/api/v1/crawler/sources",
        headers={"X-API-Key": valid_key},
        json={
            "name": "blog",
            "url": "https://example.com/blog",
            "notebook_id": "blog_kb",
            "use_browser": False,
        },
    )
    assert save_resp.status_code == 200
    assert save_resp.json()["source"]["name"] == "blog"
    source_rows = (tmp_path / "dashboard" / "crawler_sources.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(source_rows) == 1
    assert '"source_name": "blog"' in source_rows[0]

    run_resp = client.post(
        "/api/v1/crawler/run",
        headers={"X-API-Key": valid_key},
        json={"source_name": "news"},
    )
    assert run_resp.status_code == 200
    assert run_resp.json()["change_status"] == "updated"
    run_rows = (tmp_path / "dashboard" / "crawler_runs.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(run_rows) == 1
    assert '"event": "crawler.run"' in run_rows[0]


def test_rag_api_endpoints(client, valid_key, monkeypatch):
    class _FakePipeline:
        def __init__(self, data_dir, default_notebook="default", store_kind="chroma", tenant_id="default", reranker=None):
            self.data_dir = data_dir
            self.default_notebook = default_notebook
            self.store_kind = store_kind or "chroma"
            self.tenant_id = tenant_id

        async def ingest_with_metadata(
            self, source: str, *, notebook_id: str = "", metadata: dict | None = None
        ):
            return {
                "document_id": "doc-finance-1",
                "source": source,
                "notebook": notebook_id or "default",
                "tenant_id": self.tenant_id,
                "documents": 1,
                "metadata": metadata or {},
                "store_backend": self.store_kind,
            }

        def search(
            self,
            query: str,
            *,
            notebook_id: str = "",
            top_k: int = 5,
            source_contains: str = "",
            filters: dict | None = None,
            rerank: bool = False,
            rerank_top_n: int = 20,
        ):
            return {
                "query": query,
                "notebook": notebook_id or "default",
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "results": [{"content": "x", "source": source_contains or "kb.md", "score": 0.9, "page": 1}],
                "top_k": top_k,
                "filters": filters or {},
            }

        def stats(self, *, notebook_id: str = ""):
            return {
                "rag_available": True,
                "error": "",
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "backend_diagnostics": {
                    "all_healthy": True,
                    "configured_backend": self.store_kind,
                    "tenant_backend_policy": "",
                    "detected_backends": [
                        {
                            "backend": self.store_kind,
                            "healthy": True,
                            "notebook_count": 1,
                            "document_count": 1,
                            "chunk_count": 2,
                            "storage_bytes": 0,
                            "storage_present": False,
                        }
                    ],
                },
                "notebooks": [{"id": notebook_id or "default", "name": notebook_id or "default", "doc_count": 1, "chunk_count": 2, "created_at": "2026-03-13"}],
                "total_notebooks": 1,
                "total_documents": 1,
                "total_chunks": 2,
            }

        def delete_document(self, document_id: str, *, notebook_id: str = ""):
            return {
                "ok": True,
                "document_id": document_id,
                "source": "finance_policy.md",
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "notebook": notebook_id or "default",
                "notebook_id": notebook_id or "default",
                "documents_removed": 1,
                "chunks_deleted": 2,
                "metadata": {"source_type": "file", "file_ext": ".md"},
            }

        def list_documents(self, *, notebook_id: str = ""):
            return {
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "notebook": notebook_id or "default",
                "notebook_id": notebook_id or "default",
                "documents": [
                    {
                        "document_id": "doc-finance-1",
                        "source": "finance_policy.md",
                        "doc_units": 1,
                        "created_at": "2026-03-13T00:00:00+00:00",
                        "metadata": {"source_type": "file", "file_ext": ".md"},
                    }
                ],
                "total_documents": 1,
            }

        def clear_notebook(self, *, notebook_id: str = ""):
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "notebook": notebook_id or "default",
                "notebook_id": notebook_id or "default",
                "documents_removed": 1,
                "chunks_deleted": 2,
                "document_ids": ["doc-finance-1"],
            }

        def run_retention(self, *, notebook_id: str = "", max_documents: int = 0, max_age_days: int = 0):
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind,
                "notebook": notebook_id or "default",
                "notebook_id": notebook_id or "default",
                "max_documents": max_documents,
                "max_age_days": max_age_days,
                "documents_removed": 1,
                "chunks_deleted": 2,
                "document_ids": ["doc-finance-1"],
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)

    ingest = client.post(
        "/api/v1/rag/ingest",
        headers={"X-API-Key": valid_key},
        json={
            "source": "./docs",
            "notebook_id": "finance_kb",
            "tenant_id": "default",
            "store_backend": "memory",
            "metadata": {"industry": "finance"},
        },
    )
    assert ingest.status_code == 200
    assert ingest.json()["notebook"] == "finance_kb"
    assert ingest.json()["document_id"] == "doc-finance-1"
    assert ingest.json()["tenant_id"] == "default"
    assert ingest.json()["store_backend"] == "memory"
    assert ingest.json()["metadata"]["industry"] == "finance"

    search = client.post(
        "/api/v1/rag/search",
        headers={"X-API-Key": valid_key},
        json={
            "query": "policy",
            "notebook_id": "finance_kb",
            "tenant_id": "default",
            "store_backend": "memory",
            "top_k": 3,
            "source_contains": "policy",
            "filters": {"industry": "finance"},
        },
    )
    assert search.status_code == 200
    assert search.json()["query"] == "policy"
    assert search.json()["tenant_id"] == "default"
    assert search.json()["store_backend"] == "memory"
    assert search.json()["results"][0]["source"] == "policy"
    assert search.json()["filters"]["industry"] == "finance"

    stats = client.get(
        "/api/v1/rag/stats?notebook_id=finance_kb&tenant_id=default",
        headers={"X-API-Key": valid_key},
    )
    assert stats.status_code == 200
    assert stats.json()["tenant_id"] == "default"
    assert stats.json()["store_backend"] == "chroma"
    assert stats.json()["total_chunks"] == 2
    assert stats.json()["backend_diagnostics"]["all_healthy"] is True
    assert stats.json()["backend_diagnostics"]["detected_backends"][0]["backend"] == "chroma"

    documents = client.get(
        "/api/v1/rag/documents?notebook_id=finance_kb&tenant_id=default",
        headers={"X-API-Key": valid_key},
    )
    assert documents.status_code == 200
    assert documents.json()["documents"][0]["document_id"] == "doc-finance-1"
    assert documents.json()["documents"][0]["metadata"]["file_ext"] == ".md"

    delete = client.delete(
        "/api/v1/rag/doc/doc-finance-1?notebook_id=finance_kb&tenant_id=default",
        headers={"X-API-Key": valid_key},
    )
    assert delete.status_code == 200
    assert delete.json()["document_id"] == "doc-finance-1"
    assert delete.json()["documents_removed"] == 1
    assert delete.json()["metadata"]["source_type"] == "file"

    clear = client.delete(
        "/api/v1/rag/notebook/finance_kb/documents?tenant_id=default",
        headers={"X-API-Key": valid_key},
    )
    assert clear.status_code == 200
    assert clear.json()["notebook_id"] == "finance_kb"
    assert clear.json()["documents_removed"] == 1

    retention = client.post(
        "/api/v1/rag/retention/run",
        headers={"X-API-Key": valid_key},
        json={"notebook_id": "finance_kb", "tenant_id": "default", "max_documents": 1},
    )
    assert retention.status_code == 200
    assert retention.json()["notebook_id"] == "finance_kb"
    assert retention.json()["max_documents"] == 1


def test_resolve_rag_tenant_id_rejects_cross_tenant_override() -> None:
    scope = {"type": "http", "method": "GET", "path": "/api/v1/rag/stats", "headers": []}
    request = Request(scope)
    request.state.tenant_id = "tenant-a"

    with pytest.raises(Exception) as exc_info:
        _resolve_rag_tenant_id(request, "tenant-b")

    assert "cross-tenant rag access is forbidden" in str(exc_info.value)


def test_resolve_rag_tenant_id_prefers_authenticated_tenant_context() -> None:
    scope = {"type": "http", "method": "GET", "path": "/api/v1/rag/stats", "headers": []}
    request = Request(scope)
    request.state.tenant_id = "tenant-a"

    assert _resolve_rag_tenant_id(request, "") == "tenant-a"
    assert _resolve_rag_tenant_id(request, "tenant-a") == "tenant-a"


def test_rag_tenant_policy_api_endpoints(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    from zen_claw.auth.tenant import TenantStore

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    tenant = TenantStore(tmp_path).create("Acme", store_backend="memory")

    read_resp = client.get(
        f"/api/v1/rag/tenant-policy?tenant_id={tenant.tenant_id}",
        headers={"X-API-Key": valid_key},
    )
    assert read_resp.status_code == 200
    assert read_resp.json()["tenant_id"] == tenant.tenant_id
    assert read_resp.json()["store_backend"] == "memory"
    assert read_resp.json()["exists"] is True

    update_resp = client.post(
        "/api/v1/rag/tenant-policy",
        headers={"X-API-Key": valid_key},
        json={"tenant_id": tenant.tenant_id, "store_backend": "chroma"},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["tenant_id"] == tenant.tenant_id
    assert update_resp.json()["store_backend"] == "chroma"
    audit_rows = (
        tmp_path / "dashboard" / "knowledge_policy.log.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(audit_rows) == 1
    assert '"policy_kind": "tenant"' in audit_rows[0]


def test_rag_tenant_policy_api_rejects_cross_tenant_override(
    client, valid_key, monkeypatch, tmp_path: Path
) -> None:
    from zen_claw.auth.tenant import TenantStore

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    tenant_a = TenantStore(tmp_path).create("Tenant A")
    tenant_b = TenantStore(tmp_path).create("Tenant B")

    scope = {"type": "http", "method": "GET", "path": "/api/v1/rag/tenant-policy", "headers": []}
    request = Request(scope)
    request.state.tenant_id = tenant_a.tenant_id

    with pytest.raises(Exception) as exc_info:
        _resolve_rag_tenant_id(request, tenant_b.tenant_id)

    assert "cross-tenant rag access is forbidden" in str(exc_info.value)


def test_rag_notebook_policy_api_endpoints(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.knowledge.notebook import NotebookManager

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("finance_kb", store_backend="memory")

    read_resp = client.get(
        "/api/v1/rag/notebook-policy?tenant_id=default&notebook_id=finance_kb",
        headers={"X-API-Key": valid_key},
    )
    assert read_resp.status_code == 200
    assert read_resp.json()["tenant_id"] == "default"
    assert read_resp.json()["notebook_id"] == "finance_kb"
    assert read_resp.json()["store_backend"] == "memory"
    assert read_resp.json()["retention_max_documents"] == 0
    assert read_resp.json()["retention_max_age_days"] == 0
    assert read_resp.json()["exists"] is True

    update_resp = client.post(
        "/api/v1/rag/notebook-policy",
        headers={"X-API-Key": valid_key},
        json={
            "tenant_id": "default",
            "notebook_id": "finance_kb",
            "store_backend": "chroma",
            "retention_max_documents": 7,
            "retention_max_age_days": 30,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["tenant_id"] == "default"
    assert update_resp.json()["notebook_id"] == "finance_kb"
    assert update_resp.json()["store_backend"] == "chroma"
    assert update_resp.json()["retention_max_documents"] == 7
    assert update_resp.json()["retention_max_age_days"] == 30
    audit_rows = (
        tmp_path / "dashboard" / "knowledge_policy.log.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(audit_rows) == 1
    assert '"policy_kind": "notebook"' in audit_rows[0]
    assert '"detail": "retention docs: 0 -> 7; retention age: 0 -> 30"' in audit_rows[0]


def test_rag_notebook_policy_api_can_create_notebook_metadata(
    client, valid_key, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    update_resp = client.post(
        "/api/v1/rag/notebook-policy",
        headers={"X-API-Key": valid_key},
        json={
            "tenant_id": "default",
            "notebook_id": "new_kb",
            "store_backend": "memory",
            "retention_max_documents": 2,
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["notebook_id"] == "new_kb"
    assert update_resp.json()["store_backend"] == "memory"
    assert update_resp.json()["retention_max_documents"] == 2
    assert update_resp.json()["exists"] is True


def test_rag_retention_run_uses_notebook_policy_defaults(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.knowledge.notebook import NotebookManager

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    manager = NotebookManager(tenant_data_dir(tmp_path, "default"))
    manager.create("finance_kb", store_backend="memory", retention_max_documents=3, retention_max_age_days=14)

    class _FakePipeline:
        def __init__(self, data_dir, tenant_id="default", store_kind=""):
            self.tenant_id = tenant_id
            self.store_kind = store_kind

        def run_retention(self, *, notebook_id: str = "", max_documents: int = 0, max_age_days: int = 0):
            return {
                "ok": True,
                "tenant_id": self.tenant_id,
                "store_backend": self.store_kind or "memory",
                "notebook": notebook_id or "default",
                "notebook_id": notebook_id or "default",
                "max_documents": max_documents,
                "max_age_days": max_age_days,
                "documents_removed": 0,
                "chunks_deleted": 0,
                "document_ids": [],
            }

    monkeypatch.setattr("zen_claw.knowledge.pipeline.RAGPipeline", _FakePipeline)
    retention = client.post(
        "/api/v1/rag/retention/run",
        headers={"X-API-Key": valid_key},
        json={"notebook_id": "finance_kb", "tenant_id": "default"},
    )
    assert retention.status_code == 200
    assert retention.json()["max_documents"] == 3
    assert retention.json()["max_age_days"] == 14


def test_rag_policy_history_api_filter_and_csv(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    from zen_claw.auth.paths import tenant_data_dir
    from zen_claw.auth.tenant import TenantStore
    from zen_claw.knowledge.notebook import NotebookManager

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    tenant = TenantStore(tmp_path).create("Acme", store_backend="memory")
    NotebookManager(tenant_data_dir(tmp_path, tenant.tenant_id)).create(
        "finance_kb", store_backend="memory"
    )

    tenant_update = client.post(
        "/api/v1/rag/tenant-policy",
        headers={"X-API-Key": valid_key},
        json={"tenant_id": tenant.tenant_id, "store_backend": "chroma"},
    )
    assert tenant_update.status_code == 200

    notebook_update = client.post(
        "/api/v1/rag/notebook-policy",
        headers={"X-API-Key": valid_key},
        json={"tenant_id": tenant.tenant_id, "notebook_id": "finance_kb", "store_backend": "chroma"},
    )
    assert notebook_update.status_code == 200

    history_resp = client.get(
        f"/api/v1/rag/policy-history?policy_kind=tenant&tenant_id={tenant.tenant_id}&limit=5&offset=0",
        headers={"X-API-Key": valid_key},
    )
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert history["total"] == 1
    assert history["returned"] == 1
    assert history["rows"][0]["policy_kind"] == "tenant"
    assert history["rows"][0]["tenant_id"] == tenant.tenant_id

    notebook_history_resp = client.get(
        (
            f"/api/v1/rag/policy-history?policy_kind=notebook&tenant_id={tenant.tenant_id}"
            "&target_id=finance_kb&before_store_backend=memory&after_store_backend=chroma"
        ),
        headers={"X-API-Key": valid_key},
    )
    assert notebook_history_resp.status_code == 200
    notebook_history = notebook_history_resp.json()
    assert notebook_history["total"] == 1
    assert notebook_history["returned"] == 1
    assert notebook_history["target_id"] == "finance_kb"
    assert notebook_history["before_store_backend"] == "memory"
    assert notebook_history["after_store_backend"] == "chroma"
    assert notebook_history["rows"][0]["policy_kind"] == "notebook"

    csv_resp = client.get(
        f"/api/v1/rag/policy-history?tenant_id={tenant.tenant_id}&format=csv",
        headers={"X-API-Key": valid_key},
    )
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "knowledge-policy-history.csv" in csv_resp.headers["content-disposition"]
    csv_text = csv_resp.text
    assert "policy_kind,tenant_id,target_id" in csv_text
    assert tenant.tenant_id in csv_text
    assert "finance_kb" in csv_text


def test_rag_activity_history_api_filter_and_csv(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    (dashboard_dir / "knowledge_policy.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 100,
                "policy_kind": "tenant",
                "tenant_id": "acme",
                "target_id": "",
                "before_store_backend": "memory",
                "after_store_backend": "chroma",
                "actor": "api_key",
                "trace_id": "p1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (dashboard_dir / "knowledge_cron.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 200,
                "job_name": "refresh knowledge",
                "tenant_id": "acme",
                "knowledge_notebook": "finance_kb",
                "status": "error",
                "chunks_added": 0,
                "documents": 2,
                "actor": "system",
                "trace_id": "c1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resp = client.get(
        "/api/v1/rag/activity-history",
        headers={"X-API-Key": valid_key},
        params={"tenant_id": "acme", "status": "error"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["returned"] == 1
    assert body["rows"][0]["event_kind"] == "cron"
    assert body["rows"][0]["target_id"] == "finance_kb"

    csv_resp = client.get(
        "/api/v1/rag/activity-history",
        headers={"X-API-Key": valid_key},
        params={"tenant_id": "acme", "format": "csv"},
    )
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "knowledge-activity-history.csv" in csv_resp.headers["content-disposition"]
    csv_text = csv_resp.text
    assert "event_kind,tenant_id,target_id,actor,status,detail,trace_id" in csv_text
    assert "refresh knowledge" in csv_text
    assert "tenant: memory -> chroma" in csv_text


def test_ops_summary_api_json_and_csv(client, valid_key, monkeypatch, tmp_path: Path) -> None:
    from zen_claw.config.schema import Config

    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda: Config())
    monkeypatch.setattr(
        "zen_claw.runtime.sidecar_supervisor.collect_sidecar_status", lambda _cfg: []
    )

    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    (dashboard_dir / "model_routing.log.jsonl").write_text(
        json.dumps(
            {
                "at_ms": 100,
                "selected_model": "cheap-model",
                "reason": "cost_model",
                "trace_id": "t-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resp = client.get("/api/v1/ops/summary", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ops"]["model_routes"] == 1
    assert body["rows"][0]["metric"] == "status"
    assert any(row["surface"] == "model_routing" for row in body["rows"])
    assert "notes" in body
    assert "attention_sections" in body
    assert any(section["key"] == "runtime_unsupported" for section in body["attention_sections"])
    assert not any(
        section["key"].startswith("pending_apply_") and section["key"] != "pending_apply"
        for section in body["attention_sections"]
    )
    assert body["recent_activity"][0]["surface"] == "model_routing"
    assert body["pending_apply_total"] == 0

    csv_resp = client.get("/api/v1/ops/summary?format=csv", headers={"X-API-Key": valid_key})
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "operations-summary.csv" in csv_resp.headers["content-disposition"]
    csv_text = csv_resp.text
    assert "surface,metric,value" in csv_text
    assert "model_routing,recent_routes,1" in csv_text
    assert "attention_key,attention_title,attention_count,surface,at_ms,target,action,actor,detail,trace_id,pending_apply" in csv_text
    assert "recent_surface,at_ms,target,action,actor,detail,trace_id,pending_apply" in csv_text

    filtered_resp = client.get(
        "/api/v1/ops/summary?surface=model_routing&target=cheap-model&limit=1",
        headers={"X-API-Key": valid_key},
    )
    assert filtered_resp.status_code == 200
    filtered_body = filtered_resp.json()
    assert filtered_body["filters"]["surface"] == "model_routing"
    assert filtered_body["filters"]["target"] == "cheap-model"
    assert filtered_body["returned"] == 1
    assert filtered_body["recent_activity"][0]["target"] == "cheap-model"


def test_ops_apply_pending_acknowledges_agent_and_channel(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.channels.slack.enabled = True
    cfg_path = tmp_path / "config.json"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    client.post(
        "/api/v1/agents/default/planning/disable",
        headers={"X-API-Key": valid_key},
    )
    client.post(
        "/api/v1/channels/slack/disable",
        headers={"X-API-Key": valid_key},
    )

    before = client.get("/api/v1/ops/summary", headers={"X-API-Key": valid_key})
    assert before.status_code == 200
    assert before.json()["pending_apply_total"] == 2

    resp = client.post("/api/v1/ops/apply-pending", headers={"X-API-Key": valid_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["applied_count"] == 2
    assert body["pending_before"] == 2
    assert body["pending_after"] == 0
    assert body["cleared_count"] == 2
    assert body["remaining_targets"] == []
    assert {row["status"] for row in body["steps"]} == {"applied"}
    assert {row["result"] for row in body["steps"]} == {
        "audit_recorded",
        "audit_recorded_runtime_control_unsupported",
    }
    step_modes = {(row["surface"], row["execution_mode"], row["capability_status"]) for row in body["steps"]}
    assert ("agent", "audit_only", "audit_only") in step_modes
    assert ("channel", "audit_only", "runtime_control_unsupported") in step_modes
    summary_by_surface = {row["surface"]: row for row in body["surface_summary"]}
    assert summary_by_surface["agent"]["runtime_apply_target_count"] == 0
    assert summary_by_surface["agent"]["audit_only_target_count"] == 1
    assert summary_by_surface["agent"]["unsupported_target_count"] == 0
    assert summary_by_surface["channel"]["runtime_apply_target_count"] == 0
    assert summary_by_surface["channel"]["audit_only_target_count"] == 1
    assert summary_by_surface["channel"]["unsupported_target_count"] == 1
    assert {row["surface"] for row in body["rows"]} == {"agent", "channel"}

    agent_apply_rows = (tmp_path / "dashboard" / "agent_apply.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    channel_apply_rows = (tmp_path / "dashboard" / "channel_apply.log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(agent_apply_rows) == 1
    assert len(channel_apply_rows) == 1
    assert '"note": "operator_confirmed_reload_via_ops"' in agent_apply_rows[0]
    assert '"note": "operator_confirmed_reload_via_ops"' in channel_apply_rows[0]

    after = client.get("/api/v1/ops/summary?pending_only=true", headers={"X-API-Key": valid_key})
    assert after.status_code == 200
    assert after.json()["pending_apply_total"] == 0
    assert after.json()["total"] == 0

    agent_list = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    channel_list = client.get("/api/v1/channels", headers={"X-API-Key": valid_key})
    assert agent_list.json()["pending_reload"] is False
    assert channel_list.json()["pending_reload"] is False


def test_ops_apply_pending_restarts_webchat_runtime_for_channel_target(
    client, valid_key, tmp_path, monkeypatch
):
    cfg = Config()
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)

    started = {"count": 0}
    stopped = {"count": 0}

    async def _fake_start():
        started["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "running",
            "running_instances": 1,
            "known_loops": 1,
        }

    async def _fake_stop():
        stopped["count"] += 1
        return {
            "channel_name": "webchat",
            "supported": True,
            "state": "stopped",
            "running_instances": 0,
            "known_loops": 1,
            "stopped_instances": 1,
        }

    monkeypatch.setattr("zen_claw.dashboard.server._start_webchat_runtime", _fake_start)
    monkeypatch.setattr("zen_claw.dashboard.server._stop_webchat_runtime", _fake_stop)
    monkeypatch.setattr(
        "zen_claw.dashboard.server._webchat_runtime_summary",
        lambda: {
            "channel_name": "webchat",
            "supported": True,
            "state": "running",
            "running_instances": 1,
            "known_loops": 1,
        },
    )

    disable_resp = client.post("/api/v1/channels/webchat/disable", headers={"X-API-Key": valid_key})
    assert disable_resp.status_code == 200

    resp = client.post(
        "/api/v1/ops/apply-pending?surface=channel&target=webchat",
        headers={"X-API-Key": valid_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["applied_count"] == 1
    assert body["surface_summary"][0]["runtime_apply_target_count"] == 1
    assert body["surface_summary"][0]["audit_only_target_count"] == 0
    assert body["surface_summary"][0]["unsupported_target_count"] == 0
    assert body["steps"][0]["execution_mode"] == "runtime_apply_and_audit"
    assert body["steps"][0]["capability_status"] == "supported"
    assert body["steps"][0]["runtime_actions"] == ["start", "stop", "restart", "apply"]
    assert body["steps"][0]["status"] == "applied"
    assert body["steps"][0]["result"] == "runtime_restarted_and_audit_recorded"
    assert started["count"] == 1
    assert stopped["count"] == 1


def test_ops_apply_plan_previews_filtered_pending_rows(client, valid_key, tmp_path, monkeypatch):
    cfg = Config()
    cfg.channels.slack.enabled = True
    cfg_path = tmp_path / "config.json"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    client.post(
        "/api/v1/agents/default/planning/disable",
        headers={"X-API-Key": valid_key},
    )
    client.post(
        "/api/v1/channels/slack/disable",
        headers={"X-API-Key": valid_key},
    )

    resp = client.get(
        "/api/v1/ops/apply-plan?surface=agent&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["surface"] == "agent"
    assert body["matched_pending_total"] == 1
    assert body["apply_target_total"] == 1
    assert body["targets"] == ["default"]
    assert body["surface_summary"][0]["surface"] == "agent"
    assert body["surface_summary"][0]["target_count"] == 1
    assert body["surface_summary"][0]["targets"] == ["default"]
    assert body["surface_summary"][0]["runtime_apply_target_count"] == 0
    assert body["surface_summary"][0]["audit_only_target_count"] == 1
    assert body["surface_summary"][0]["unsupported_target_count"] == 0
    assert body["steps"][0]["step"] == 1
    assert body["steps"][0]["surface"] == "agent"
    assert body["steps"][0]["target"] == "default"
    assert body["steps"][0]["execution_mode"] == "audit_only"
    assert body["steps"][0]["capability_status"] == "audit_only"
    assert body["rows"][0]["surface"] == "agent"
    assert body["rows"][0]["target"] == "default"
    assert body["rows"][0]["execution_mode"] == "audit_only"
    assert body["rows"][0]["capability_status"] == "audit_only"


def test_ops_apply_pending_can_scope_to_one_agent_target(client, valid_key, tmp_path, monkeypatch):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                    }
                },
            }
        }
    )
    cfg_path = tmp_path / "config.json"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    client.post("/api/v1/agents/default/planning/disable", headers={"X-API-Key": valid_key})
    client.post(
        "/api/v1/agents/finance_writer/planning/disable",
        headers={"X-API-Key": valid_key},
    )

    before = client.get(
        "/api/v1/ops/summary?surface=agent&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert before.status_code == 200
    assert before.json()["pending_apply_total"] == 2

    resp = client.post(
        "/api/v1/ops/apply-pending?surface=agent&target=finance_writer",
        headers={"X-API-Key": valid_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["surface"] == "agent"
    assert body["target"] == "finance_writer"
    assert body["applied_count"] == 1
    assert body["pending_before"] == 1
    assert body["pending_after"] == 0
    assert body["cleared_count"] == 1
    assert body["remaining_targets"] == []
    assert body["surface_summary"][0]["surface"] == "agent"
    assert body["surface_summary"][0]["target_count"] == 1
    assert body["surface_summary"][0]["targets"] == ["finance_writer"]
    assert body["steps"][0]["step"] == 1
    assert body["steps"][0]["surface"] == "agent"
    assert body["steps"][0]["target"] == "finance_writer"
    assert body["steps"][0]["status"] == "applied"
    assert body["rows"][0]["surface"] == "agent"
    assert body["rows"][0]["event"]["target"] == "finance_writer"

    filtered = client.get(
        "/api/v1/ops/summary?surface=agent&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["pending_apply_total"] == 1
    assert filtered_body["recent_activity"][0]["target"] == "default"
    assert all(row["target"] != "finance_writer" for row in filtered_body["recent_activity"])

    targeted = client.get(
        "/api/v1/ops/summary?surface=agent&target=finance_writer&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert targeted.status_code == 200
    assert targeted.json()["pending_apply_total"] == 0
    assert targeted.json()["total"] == 0

    agent_list = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert agent_list.status_code == 200
    assert agent_list.json()["pending_reload"] is True
    assert agent_list.json()["pending_reload_count"] == 1


def test_ops_apply_pending_can_scope_to_actor_filtered_pending_rows(
    client, valid_key, tmp_path, monkeypatch
):
    cfg = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path / "default-ws"),
                    "model": "default-model",
                },
                "profiles": {
                    "finance_writer": {
                        "display_name": "Finance Writer",
                        "model": "deepseek-chat",
                    }
                },
            }
        }
    )
    cfg_path = tmp_path / "config.json"

    monkeypatch.setattr("zen_claw.config.loader.load_config", lambda path=None: cfg)
    monkeypatch.setattr("zen_claw.config.loader.get_config_path", lambda: cfg_path)
    monkeypatch.setattr("zen_claw.config.loader.get_data_dir", lambda: tmp_path)

    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    (dashboard_dir / "agent_actions.log.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "at_ms": 100,
                        "agent_id": "default",
                        "action": "planning_disable",
                        "detail": "planning -> false",
                        "actor": "api_key",
                    }
                ),
                json.dumps(
                    {
                        "at_ms": 200,
                        "agent_id": "finance_writer",
                        "action": "planning_disable",
                        "detail": "planning -> false",
                        "actor": "ops_reviewer",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    before = client.get(
        "/api/v1/ops/summary?surface=agent&actor=ops_reviewer&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert before.status_code == 200
    assert before.json()["pending_apply_total"] == 1
    assert before.json()["pending_apply"][0]["target"] == "finance_writer"

    resp = client.post(
        "/api/v1/ops/apply-pending?surface=agent&actor=ops_reviewer",
        headers={"X-API-Key": valid_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["surface"] == "agent"
    assert body["actor_filter"] == "ops_reviewer"
    assert body["matched_pending_total"] == 1
    assert body["applied_count"] == 1
    assert body["pending_before"] == 1
    assert body["pending_after"] == 0
    assert body["cleared_count"] == 1
    assert body["remaining_targets"] == []
    assert body["steps"][0]["step"] == 1
    assert body["steps"][0]["status"] == "applied"
    assert body["rows"][0]["surface"] == "agent"
    assert body["rows"][0]["event"]["target"] == "finance_writer"

    after_filtered = client.get(
        "/api/v1/ops/summary?surface=agent&actor=ops_reviewer&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert after_filtered.status_code == 200
    assert after_filtered.json()["pending_apply_total"] == 0
    assert after_filtered.json()["total"] == 0

    after_all = client.get(
        "/api/v1/ops/summary?surface=agent&pending_only=true",
        headers={"X-API-Key": valid_key},
    )
    assert after_all.status_code == 200
    assert after_all.json()["pending_apply_total"] == 1
    assert after_all.json()["pending_apply"][0]["target"] == "default"

    agent_list = client.get("/api/v1/agents", headers={"X-API-Key": valid_key})
    assert agent_list.status_code == 200
    assert agent_list.json()["pending_reload"] is True
    assert agent_list.json()["pending_reload_count"] == 1


def test_chat_ws_connects(client):
    with client.websocket_connect("/chat/ws/test-session-001") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "hello"}))
        msg = ws.receive_text()
        data = json.loads(msg)
        assert data["type"] in ("token", "done", "error")


def test_chat_ws_uses_webchat_runtime_when_available(client, monkeypatch):
    from zen_claw.dashboard import server as srv

    class _FakeChannel:
        def __init__(self):
            self.last_content = ""

        async def ingest_user_message(self, **kwargs):
            self.last_content = str(kwargs.get("content", ""))

        async def pop_response(self, _session_id: str, timeout_sec: float = 0.0):
            _ = timeout_sec
            return OutboundMessage(
                channel="webchat", chat_id="s", content=f"bus:{self.last_content}"
            )

    class _Cfg:
        class _Channels:
            class _Webchat:
                token = ""

            webchat = _Webchat()

        channels = _Channels()

    class _FakeRuntime:
        def __init__(self):
            self.cfg = _Cfg()
            self.channel = _FakeChannel()

    async def _fake_runtime():
        return _FakeRuntime()

    monkeypatch.setattr(srv, "_get_webchat_runtime", _fake_runtime)

    with client.websocket_connect("/chat/ws/test-session-002") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "hello-bus"}))
        tokens: list[str] = []
        for _ in range(64):
            data = json.loads(ws.receive_text())
            if data.get("type") == "token":
                tokens.append(str(data.get("content", "")))
                continue
            if data.get("type") == "done":
                break
        assert "".join(tokens).startswith("bus:hello-bus")
