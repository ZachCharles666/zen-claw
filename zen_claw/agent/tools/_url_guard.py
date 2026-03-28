"""Shared URL / domain guard utilities used by browser and web tools."""

from urllib.parse import urlparse


def is_domain_blocked(url: str, blocked_domains: list[str]) -> bool:
    """
    Return True if the URL's hostname matches any entry in blocked_domains.

    Matching rules:
    - Exact hostname match: "evil.com" blocks requests to "evil.com"
    - Subdomain match: "evil.com" also blocks "sub.evil.com"
    - Wildcard prefix ("*.evil.com") treated same as "evil.com"
    - Case-insensitive comparison

    Args:
        url: The URL to check (http/https or bare hostname).
        blocked_domains: List of domain patterns to block.

    Returns:
        True if the URL should be blocked.
    """
    if not blocked_domains or not url:
        return False

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # If urlparse couldn't extract a hostname, treat url as a bare hostname
        if not host:
            host = url.split("/")[0].split(":")[0]
    except Exception:
        return False

    host = host.lower().rstrip(".")
    if not host:
        return False

    for pattern in blocked_domains:
        # Normalise: strip wildcard prefix and leading dots
        normalised = pattern.lower().lstrip("*").lstrip(".").rstrip(".")
        if not normalised:
            continue
        if host == normalised or host.endswith("." + normalised):
            return True

    return False
