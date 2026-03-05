"""Tests for BaseChannel.is_allowed() default-deny policy (MEDIUM-003).

Before the fix, an empty config (no admins/users/allow_from) returned True,
allowing every sender.  After the fix it returns False and logs a warning.
"""

import asyncio
from types import SimpleNamespace

import pytest

from zen_claw.bus.events import OutboundMessage
from zen_claw.bus.queue import MessageBus
from zen_claw.channels.base import BaseChannel

# ── minimal concrete channel for testing ─────────────────────────────────────


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> None:
        pass


def _chan(config) -> _DummyChannel:
    return _DummyChannel(config, MessageBus())


# ── tests: default-deny (MEDIUM-003 core fix) ─────────────────────────────────


def test_empty_config_denies_all_senders():
    """No admins/users/allow_from → deny everyone (was: allow everyone)."""
    ch = _chan(SimpleNamespace(admins=[], users=[], allow_from=[]))
    assert ch.is_allowed("any-user") is False
    assert ch.is_allowed("12345") is False


def test_empty_config_logs_warning():
    """Default-deny path must call logger.warning() so operators are notified.
    We patch the loguru logger used in base.py to verify the call."""
    from unittest.mock import patch

    ch = _chan(SimpleNamespace(admins=[], users=[], allow_from=[]))
    with patch("zen_claw.channels.base.logger") as mock_logger:
        result = ch.is_allowed("stranger")

    assert result is False
    mock_logger.warning.assert_called_once()
    # The message should mention the lack of configuration and denial
    call_args = " ".join(str(a) for a in mock_logger.warning.call_args.args)
    assert "deny" in call_args.lower() or "no admins" in call_args.lower()


def test_empty_config_blocks_handle_message():
    """_handle_message must NOT publish when sender is denied by default-deny."""
    bus = MessageBus()
    ch = _chan(SimpleNamespace(admins=[], users=[], allow_from=[]))
    asyncio.run(ch._handle_message(sender_id="stranger", chat_id="c1", content="hello"))
    # Queue should be empty — message was blocked
    with pytest.raises(Exception):
        asyncio.run(asyncio.wait_for(bus.consume_inbound(), timeout=0.05))


# ── tests: explicit allow paths still work ───────────────────────────────────


def test_admins_list_allows_listed_sender():
    ch = _chan(SimpleNamespace(admins=["alice"], users=[], allow_from=[]))
    assert ch.is_allowed("alice") is True
    assert ch.is_allowed("bob") is False


def test_users_list_allows_listed_sender():
    ch = _chan(SimpleNamespace(admins=[], users=["bob"], allow_from=[]))
    assert ch.is_allowed("bob") is True
    assert ch.is_allowed("alice") is False


def test_allow_from_list_allows_listed_sender():
    ch = _chan(SimpleNamespace(admins=[], users=[], allow_from=["carol"]))
    assert ch.is_allowed("carol") is True
    assert ch.is_allowed("dave") is False


def test_admins_takes_priority_over_allow_from():
    """When admins/users set, allow_from is ignored (RBAC mode)."""
    ch = _chan(SimpleNamespace(admins=["admin1"], users=[], allow_from=["legacy"]))
    assert ch.is_allowed("admin1") is True
    assert ch.is_allowed("legacy") is False  # allow_from ignored in RBAC mode


def test_access_checker_callback_overrides_default_deny():
    """If access_checker is set, it takes full precedence over the config lists."""
    ch = _chan(SimpleNamespace(admins=[], users=[], allow_from=[]))
    ch.access_checker = lambda sender_id, cfg: sender_id == "special"
    assert ch.is_allowed("special") is True
    assert ch.is_allowed("nobody") is False


def test_compound_sender_token_matched_against_admins():
    """'123|alice' should match 'alice' in admins via token splitting."""
    ch = _chan(SimpleNamespace(admins=["alice"], users=[], allow_from=[]))
    assert ch.is_allowed("123|alice") is True
    assert ch.is_allowed("123|unknown") is False
