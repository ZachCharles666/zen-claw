"""Tests for SSRF protection in web tools (IPv4 and IPv6)."""

import pytest

from zen_claw.agent.tools.web import _is_private_host, _validate_url


# ---------------------------------------------------------------------------
# IPv4 private ranges
# ---------------------------------------------------------------------------


def test_ipv4_loopback_blocked():
    assert _is_private_host("127.0.0.1") is True


def test_ipv4_rfc1918_10_blocked():
    assert _is_private_host("10.0.0.1") is True


def test_ipv4_rfc1918_172_blocked():
    assert _is_private_host("172.16.0.1") is True


def test_ipv4_rfc1918_192_blocked():
    assert _is_private_host("192.168.1.1") is True


def test_ipv4_link_local_blocked():
    assert _is_private_host("169.254.169.254") is True  # AWS metadata endpoint


def test_ipv4_public_allowed():
    assert _is_private_host("8.8.8.8") is False


# ---------------------------------------------------------------------------
# IPv6 private / reserved ranges
# ---------------------------------------------------------------------------


def test_ipv6_loopback_blocked():
    assert _is_private_host("::1") is True


def test_ipv6_link_local_blocked():
    assert _is_private_host("fe80::1") is True


def test_ipv6_link_local_blocked_full():
    assert _is_private_host("fe80::abcd:ef12:3456:7890") is True


def test_ipv6_unique_local_fd_blocked():
    assert _is_private_host("fd00::1") is True


def test_ipv6_unique_local_fc_blocked():
    assert _is_private_host("fc00::1") is True


def test_ipv6_public_allowed():
    # 2001:db8::/32 is documentation range but NOT in our block list — treated as public
    # Use a clearly public address instead
    assert _is_private_host("2606:4700:4700::1111") is False  # Cloudflare DNS


# ---------------------------------------------------------------------------
# _validate_url end-to-end
# ---------------------------------------------------------------------------


def test_validate_url_rejects_ipv6_loopback():
    ok, msg = _validate_url("http://[::1]/etc/passwd")
    assert not ok
    assert "private" in msg.lower() or "reserved" in msg.lower()


def test_validate_url_rejects_ipv6_link_local():
    ok, msg = _validate_url("http://[fe80::1]/")
    assert not ok


def test_validate_url_rejects_ipv4_loopback():
    ok, msg = _validate_url("http://127.0.0.1/secret")
    assert not ok


def test_validate_url_rejects_non_http_scheme():
    ok, msg = _validate_url("ftp://example.com/file")
    assert not ok
    assert "http" in msg.lower()


def test_validate_url_accepts_public_https():
    ok, msg = _validate_url("https://example.com/page")
    assert ok, msg
