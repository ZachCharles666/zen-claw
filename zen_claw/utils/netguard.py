"""Network safety helpers for outbound host validation."""

from __future__ import annotations

import ipaddress
import socket


def is_public_ip(ip_str: str) -> bool:
    """Return True only for routable public IP addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_safe_ip(host: str) -> str | None:
    """Resolve a host and return the first safe public IP, else None."""
    if not host or host.lower() == "localhost":
        return None
    try:
        addr_info = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC)
    except socket.gaierror:
        return None
    for info in addr_info:
        ip = info[4][0]
        if is_public_ip(ip):
            return ip
    return None
