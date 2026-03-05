import hashlib
import hmac
import os
from pathlib import Path


def get_hmac_master_key() -> bytes:
    """
    Derive a stable HMAC master key from environment or machine-specific seeds.
    In K8s/Production, use ZEN_CLAW_HMAC_MASTER_KEY.
    In Local Dev, fallback to machine-id + pepper.
    """
    env_key = os.environ.get("ZEN_CLAW_HMAC_MASTER_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    # Fallback for local development
    seeds = [b"zen-claw-default-pepper"]
    # Attempt to use machine-id on Linux
    if os.path.exists("/etc/machine-id"):
        try:
            seeds.append(Path("/etc/machine-id").read_bytes().strip())
        except Exception:
            pass
    # Attempt to use a local persistent seed file
    seed_file = Path.home() / ".zen-claw" / ".hmac_seed"
    if not seed_file.exists():
        try:
            seed_file.parent.mkdir(parents=True, exist_ok=True)
            seed_file.write_bytes(os.urandom(32))
        except Exception:
            pass
    if seed_file.exists():
        try:
            seeds.append(seed_file.read_bytes())
        except Exception:
            pass

    # Simple HKDF-like derivation using SHA256
    combined = b"".join(seeds)
    return hashlib.sha256(combined).digest()


def sign_data(data: str, master_key: bytes | None = None) -> str:
    """Sign a string using HMAC-SHA256."""
    if master_key is None:
        master_key = get_hmac_master_key()
    return hmac.new(master_key, data.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(data: str, signature: str, master_key: bytes | None = None) -> bool:
    """Verify an HMAC-SHA256 signature."""
    if master_key is None:
        master_key = get_hmac_master_key()
    expected = sign_data(data, master_key)
    return hmac.compare_digest(signature, expected)
