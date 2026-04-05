from zen_claw.agent.router import AgentRouter
from zen_claw.config.loader import convert_keys
from zen_claw.config.schema import Config


def test_agent_router_prefers_explicit_then_bound_then_keyword_then_channel_default() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "profiles": {
                        "finance_writer": {"routingKeywords": ["finance", "bank campaign"]},
                        "realty_writer": {"routingKeywords": ["real estate", "property launch"]},
                    }
                },
                "channels": {"webchat": {"agentProfile": "realty_writer"}},
            }
        )
    )
    bound = {("webchat", "chat-1", "user-1"): "finance_writer"}
    router = AgentRouter(
        cfg,
        resolve_bound_agent=lambda channel, chat_id, user_id: bound.get((channel, chat_id, user_id)),
    )

    explicit = router.resolve(
        channel="webchat",
        chat_id="chat-1",
        user_id="user-1",
        content="property launch plan",
        explicit_agent_id="alpha",
    )
    assert explicit.agent_id == "alpha"
    assert explicit.reason == "explicit"

    sticky = router.resolve(
        channel="webchat",
        chat_id="chat-1",
        user_id="user-1",
        content="property launch plan",
    )
    assert sticky.agent_id == "finance_writer"
    assert sticky.reason == "bound_route"

    keyword = router.resolve(
        channel="webchat",
        chat_id="chat-2",
        user_id="user-2",
        content="need a bank campaign draft",
    )
    assert keyword.agent_id == "finance_writer"
    assert keyword.reason == "keyword"
    assert keyword.matched_keyword == "bank campaign"

    channel_default = router.resolve(
        channel="webchat",
        chat_id="chat-3",
        user_id="user-3",
        content="hello there",
    )
    assert channel_default.agent_id == "realty_writer"
    assert channel_default.reason == "channel_profile"

    fallback = router.resolve(
        channel="cli",
        chat_id="chat-4",
        user_id="user-4",
        content="hello there",
    )
    assert fallback.agent_id == "default"
    assert fallback.reason == "fallback_default"


def test_agent_router_uses_longest_keyword_match() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "agents": {
                    "profiles": {
                        "finance_writer": {"routingKeywords": ["finance", "finance report"]},
                    }
                }
            }
        )
    )
    router = AgentRouter(cfg)
    decision = router.resolve(
        channel="cli",
        chat_id="c1",
        user_id="u1",
        content="please write a finance report for this quarter",
    )
    assert decision.agent_id == "finance_writer"
    assert decision.reason == "keyword"
    assert decision.matched_keyword == "finance report"


def test_agent_router_blocks_dev_profile_on_remote_channels() -> None:
    cfg = Config.model_validate(
        convert_keys(
            {
                "tools": {"policy": {"trustedLocalChannels": ["cli", "system"]}},
                "agents": {
                    "profiles": {
                        "dev_shell": {
                            "devProfile": True,
                            "trustedLocalOnly": True,
                            "allowedChannels": ["cli"],
                        }
                    }
                },
                "channels": {"webchat": {"agentProfile": "dev_shell"}},
            }
        )
    )
    router = AgentRouter(cfg)

    local = router.resolve(
        channel="cli",
        chat_id="chat-1",
        user_id="user-1",
        content="hello",
        explicit_agent_id="dev_shell",
    )
    assert local.agent_id == "dev_shell"
    assert local.reason == "explicit"

    remote = router.resolve(
        channel="webchat",
        chat_id="chat-2",
        user_id="user-2",
        content="hello",
        explicit_agent_id="dev_shell",
    )
    assert remote.agent_id == "default"
    assert remote.reason == "explicit_denied"

    fallback = router.resolve(
        channel="webchat",
        chat_id="chat-3",
        user_id="user-3",
        content="hello",
    )
    assert fallback.agent_id == "default"
    assert fallback.reason == "channel_profile"
