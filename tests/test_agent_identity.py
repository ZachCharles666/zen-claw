import json
import os
import stat
from pathlib import Path

import pytest

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: F401

    _CRYPTO_AVAILABLE = True
except Exception:
    _CRYPTO_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _CRYPTO_AVAILABLE, reason="cryptography not installed")

from zen_claw.auth.identity import AgentIdentity, AgentIdentityError  # noqa: E402


def test_keypair_created_on_first_call(tmp_path: Path):
    key_dir = tmp_path / ".agent_keys"
    identity = AgentIdentity(key_dir)
    pub_hex, priv_path = identity.get_or_create_keypair()
    assert priv_path.exists()
    assert (key_dir / "identity.pub").exists()
    assert len(pub_hex) == 64
    assert all(c in "0123456789abcdef" for c in pub_hex)


def test_keypair_persists_across_instances(tmp_path: Path):
    key_dir = tmp_path / ".agent_keys"
    pub1, _ = AgentIdentity(key_dir).get_or_create_keypair()
    pub2, _ = AgentIdentity(key_dir).get_or_create_keypair()
    assert pub1 == pub2


def test_keypair_created_at_timestamp(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    identity.get_or_create_keypair()
    created_at = identity.created_at()
    assert created_at is not None
    from datetime import datetime

    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    assert dt.year >= 2020


def test_public_metadata_file_contains_correct_hex(tmp_path: Path):
    key_dir = tmp_path / ".agent_keys"
    pub_hex, _ = AgentIdentity(key_dir).get_or_create_keypair()
    meta = json.loads((key_dir / "identity.pub").read_text(encoding="utf-8"))
    assert meta["public_key_hex"] == pub_hex
    assert meta["algorithm"] == "ed25519"


def test_sign_verify_roundtrip(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    identity.get_or_create_keypair()
    msg = b"hello"
    sig = identity.sign(msg)
    assert AgentIdentity.verify(identity.public_key_hex(), msg, sig) is True


def test_sign_verify_unicode_message(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    identity.get_or_create_keypair()
    msg = "你好，世界".encode("utf-8")
    sig = identity.sign(msg)
    assert AgentIdentity.verify(identity.public_key_hex(), msg, sig) is True


def test_verify_wrong_message_returns_false(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    identity.get_or_create_keypair()
    sig = identity.sign(b"orig")
    assert AgentIdentity.verify(identity.public_key_hex(), b"tampered", sig) is False


def test_verify_wrong_key_returns_false(tmp_path: Path):
    id1 = AgentIdentity(tmp_path / "k1")
    id2 = AgentIdentity(tmp_path / "k2")
    id1.get_or_create_keypair()
    id2.get_or_create_keypair()
    sig = id1.sign(b"msg")
    assert AgentIdentity.verify(id2.public_key_hex(), b"msg", sig) is False


def test_verify_corrupted_signature_returns_false(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    identity.get_or_create_keypair()
    pub_hex = identity.public_key_hex()
    assert AgentIdentity.verify(pub_hex, b"msg", "not_a_real_sig") is False
    assert AgentIdentity.verify(pub_hex, b"msg", "") is False
    assert AgentIdentity.verify("deadbeef" * 8, b"msg", "AAAA") is False


def test_verify_wrong_pubkey_length_returns_false():
    assert AgentIdentity.verify("abcd", b"msg", "AAAA") is False


def test_sign_without_loading_raises(tmp_path: Path):
    identity = AgentIdentity(tmp_path / ".agent_keys")
    with pytest.raises(AgentIdentityError):
        identity.sign(b"message")


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_key_file_permissions_are_restrictive(tmp_path: Path):
    _, priv_path = AgentIdentity(tmp_path / ".agent_keys").get_or_create_keypair()
    assert stat.S_IMODE(priv_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_load_insecure_key_raises(tmp_path: Path):
    key_dir = tmp_path / ".agent_keys"
    _, priv_path = AgentIdentity(key_dir).get_or_create_keypair()
    os.chmod(priv_path, 0o644)
    with pytest.raises(AgentIdentityError, match="insecure permissions"):
        AgentIdentity(key_dir).get_or_create_keypair()


async def test_agent_sign_tool_success(tmp_path: Path):
    from zen_claw.agent.tools.identity import AgentSignTool

    tool = AgentSignTool(workspace=tmp_path)
    result = await tool.execute(message="Hello from tool")
    assert result.ok is True
    data = json.loads(result.content)
    assert "signature" in data
    assert "public_key" in data
    assert AgentIdentity.verify(data["public_key"], b"Hello from tool", data["signature"]) is True


async def test_agent_sign_tool_empty_message(tmp_path: Path):
    from zen_claw.agent.tools.identity import AgentSignTool
    from zen_claw.agent.tools.result import ToolErrorKind

    tool = AgentSignTool(workspace=tmp_path)
    result = await tool.execute(message="")
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PARAMETER


async def test_agent_public_key_tool(tmp_path: Path):
    from zen_claw.agent.tools.identity import AgentPublicKeyTool

    tool = AgentPublicKeyTool(workspace=tmp_path)
    r1 = await tool.execute()
    r2 = await tool.execute()
    d1 = json.loads(r1.content)
    d2 = json.loads(r2.content)
    assert r1.ok is True and r2.ok is True
    assert len(d1["public_key"]) == 64
    assert d1["public_key"] == d2["public_key"]


async def test_agent_verify_tool_success(tmp_path):
    from zen_claw.agent.tools.identity import AgentSignTool, AgentVerifyTool

    sign_tool = AgentSignTool(workspace=tmp_path)
    sign_res = await sign_tool.execute(message="Verify me")
    import json

    data = json.loads(sign_res.content)
    verify_tool = AgentVerifyTool(workspace=tmp_path)
    ver_res = await verify_tool.execute(
        public_key=data["public_key"], message="Verify me", signature=data["signature"]
    )
    assert ver_res.ok is True
    assert json.loads(ver_res.content)["valid"] is True


async def test_agent_verify_tool_failure(tmp_path):
    from zen_claw.agent.tools.identity import AgentSignTool, AgentVerifyTool

    sign_tool = AgentSignTool(workspace=tmp_path)
    sign_res = await sign_tool.execute(message="Verify me")
    import json

    data = json.loads(sign_res.content)
    verify_tool = AgentVerifyTool(workspace=tmp_path)
    ver_res = await verify_tool.execute(
        public_key=data["public_key"], message="Tampered message", signature=data["signature"]
    )
    assert ver_res.ok is True
    assert json.loads(ver_res.content)["valid"] is False
