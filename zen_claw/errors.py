"""Unified error taxonomy for Zen-Claw."""

from enum import Enum


class ErrorCode(Enum):
    SIGNATURE_INVALID = "signature_invalid"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    SECURITY_VIOLATION = "security_violation"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    QUOTA_EXCEEDED = "quota_exceeded"
    SNAPSHOT_EXPIRED = "snapshot_expired"
    INSTALL_FAILED = "install_failed"
    BOOTSTRAP_FAILURE = "bootstrap_failure"


class ZenClawError(Exception):
    """Base error for all Zen-Claw exceptions."""

    def __init__(self, message: str, code: ErrorCode | str | None = None):
        super().__init__(message)
        self.code = code


class SecurityError(ZenClawError):
    """Raised when a security policy is violated."""

    pass


class AgentMidTurnReloadError(ZenClawError):
    """
    Exception raised when a skill is installed mid-turn,
    forcing the agent loop to reload and rebuild context.
    """

    def __init__(self, message: str, pins: dict[str, str] | None = None):
        super().__init__(message)
        self.pins = pins


# Backward-compatible aliases.
SecurityException = SecurityError
AgentMidTurnReloadException = AgentMidTurnReloadError
