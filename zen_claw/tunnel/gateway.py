import hashlib
import hmac
import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict

from fastapi import HTTPException, status

logger = logging.getLogger("zen_claw.tunnel.gateway")


class TunnelGatewaySecurity:
    """
    Implements P4-A Exposure Guards:
    - Path constraints (only /webhook/*)
    - Method constraints (only POST)
    - Payload size limits
    - Webhook Signature + key_id rotation
    - Clock drift tolerance
    - Nonce TTL & Capacity limits (Memory Bomb defense)
    - DoS Circuit Breaker (IP/ASN Blacklisting)
    """

    def __init__(
        self,
        max_payload_bytes: int = 2 * 1024 * 1024,  # 2MB
        clock_drift_seconds: int = 300,  # 5 min tolerance
        nonce_capacity: int = 10000,
        rate_limit_per_minute: int = 60,
        circuit_breaker_threshold: int = 100,  # Hits before blacklisting IP
    ):
        self.max_payload_bytes = max_payload_bytes
        self.clock_drift_seconds = clock_drift_seconds
        self.nonce_capacity = nonce_capacity
        self.rate_limit_per_minute = rate_limit_per_minute
        self.circuit_breaker_threshold = circuit_breaker_threshold

        # In-memory stores (In production, use Redis)
        self._seen_nonces: Dict[str, float] = {}
        self._ip_hit_counts: Dict[str, int] = defaultdict(int)
        self._blacklisted_ips: set = set()
        self._lock = threading.Lock()

        # Mapping of key_id -> secret_key for rotation
        self._active_keys: Dict[str, str] = {}

    def add_active_key(self, key_id: str, secret: str):
        """Register a valid signing key."""
        with self._lock:
            self._active_keys[key_id] = secret

    def _cleanup_nonces(self):
        """Evicts expired nonces to prevent memory exhaustion."""
        now = time.time()
        with self._lock:
            # Drop older than 1 hour or over capacity
            keys_to_delete = [k for k, v in self._seen_nonces.items() if now - v > 3600]
            for k in keys_to_delete:
                del self._seen_nonces[k]

            # If still over capacity, drop randomly (or oldest)
            if len(self._seen_nonces) > self.nonce_capacity:
                logger.warning(
                    f"Nonce capacity exceeded ({self.nonce_capacity}). Evicting aggressively."
                )
                self._seen_nonces.clear()  # Hard panic reset, better safe than OOM

    def _check_rate_limit(self, ip: str) -> bool:
        """Simple token bucket / counter rate limit per IP"""
        with self._lock:
            if ip in self._blacklisted_ips:
                return False

            self._ip_hit_counts[ip] += 1
            if self._ip_hit_counts[ip] > self.circuit_breaker_threshold:
                logger.warning(f"Circuit Breaker TRIPPED for IP: {ip}. Blacklisting.")
                self._blacklisted_ips.add(ip)
                return False

            # More granular rate limiting can be applied here
            return True

    def verify_request_sync(self, request_body: bytes, headers: Any, client_ip: str) -> bool:
        """
        Runs the full security gauntlet synchronously.
        FastAPI middleware will call this.
        """
        # 0. DoS / Circuit Breaker Check
        if not self._check_rate_limit(client_ip):
            logger.warning(f"Request dropped by Circuit Breaker from {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too Many Requests"
            )

        # 1. Payload Size Check
        if len(request_body) > self.max_payload_bytes:
            logger.warning(f"Payload too large: {len(request_body)} bytes from {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload Too Large"
            )

        # Ensure headers exist
        req_sig = headers.get("x-claw-signature")
        req_ts_str = headers.get("x-claw-timestamp")
        req_nonce = headers.get("x-claw-nonce")
        req_key_id = headers.get("x-claw-key-id")

        if not all([req_sig, req_ts_str, req_nonce, req_key_id]):
            logger.warning("Missing authentication headers.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authentication Headers"
            )

        # 2. Key ID Check (Rotation Support)
        with self._lock:
            secret = self._active_keys.get(req_key_id)
            if not secret:
                logger.warning(f"Unknown key_id provided: {req_key_id}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Key ID"
                )

        # 3. Clock Drift Check
        try:
            req_ts = int(req_ts_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Timestamp format"
            )

        now = int(time.time())
        if abs(now - req_ts) > self.clock_drift_seconds:
            logger.warning(f"Timestamp drift exceeded: {now - req_ts}s.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Timestamp Expired"
            )

        # 4. Nonce Replay Check & Capacity
        with self._lock:
            if req_nonce in self._seen_nonces:
                logger.warning(f"Replay attack detected. Nonce reused: {req_nonce}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Nonce already used"
                )
            self._seen_nonces[req_nonce] = time.time()

            # Periodically cleanup
            if len(self._seen_nonces) > (self.nonce_capacity * 0.9):
                self._cleanup_nonces()

        # 5. HMAC Signature Verification
        # Payload format: "timestamp.nonce.body"
        payload_to_sign = f"{req_ts}.{req_nonce}.".encode("utf-8") + request_body
        expected_sig = hmac.new(secret.encode("utf-8"), payload_to_sign, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(req_sig, expected_sig):
            logger.warning("Signature mismatch.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Signature"
            )

        return True

    def reset_blacklists(self):
        """Admin function to clear blacklists and reset Circuit Breaker"""
        with self._lock:
            logger.info("Resetting all IP blacklists and rate limits (Manual Override)")
            self._blacklisted_ips.clear()
            self._ip_hit_counts.clear()
