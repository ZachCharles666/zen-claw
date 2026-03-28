"""Tests for install_skill_by_snapshot() remote download implementation."""

import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zen_claw.agent.skills import SkillsLoader
from zen_claw.errors import AgentMidTurnReloadException


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_skills_loader_io(monkeypatch):
    monkeypatch.setattr(SkillsLoader, "_load_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_save_skill_mapping", lambda self: None)
    monkeypatch.setattr(SkillsLoader, "_now_ts", lambda self: 1000.0)
    monkeypatch.setattr(SkillsLoader, "_journal_recovery", lambda self: None)


def _make_loader(workspace: Path) -> SkillsLoader:
    return SkillsLoader(workspace)


def _build_skill_zip() -> tuple[bytes, str]:
    """Build a minimal valid skill zip and return (zip_bytes, sha256_hex)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("myskill/SKILL.md", "# myskill\nA test skill.")
        zf.writestr(
            "myskill/manifest.json",
            json.dumps(
                {
                    "name": "myskill",
                    "version": "1.0.0",
                    "description": "test",
                    "permissions": [],
                    "trust": "untrusted",
                }
            ),
        )
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    return data, digest


def _register_snapshot(loader: SkillsLoader, download_url: str, digest: str) -> str:
    """Register a fake snapshot and return its snapshot_id."""
    now = 1000.0
    snapshot = {
        "name": "myskill",
        "version": "1.0.0",
        "digest": digest,
        "publisher": "test",
        "download_url": download_url,
        "issued_at": now,
        "expires_at": now + loader.MAX_SNAPSHOT_AGE,
        "nonce": "deadbeef01020304",
    }
    sid = loader._sign_snapshot(snapshot)
    loader._snapshots[sid] = snapshot
    return sid


def _make_httpx_mock(zip_bytes: bytes, status: int = 200):
    """Return a mock AsyncClient whose stream() yields zip_bytes in one chunk."""

    async def _aiter_bytes(chunk_size=65536):
        yield zip_bytes

    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.aiter_bytes = _aiter_bytes

    @asynccontextmanager
    async def _stream(*args, **kwargs):
        yield mock_response

    mock_client = MagicMock()
    mock_client.stream = _stream
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_not_found(tmp_path):
    loader = _make_loader(tmp_path)
    ok, msg = await loader.install_skill_by_snapshot("nonexistent-id")
    assert not ok
    assert "invalid or expired" in msg


@pytest.mark.asyncio
async def test_snapshot_expired(tmp_path, monkeypatch):
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "https://example.com/skill.zip", digest)
    # Wind clock past expiry
    monkeypatch.setattr(loader, "_now_ts", lambda: 1000.0 + loader.MAX_SNAPSHOT_AGE + 1)
    ok, msg = await loader.install_skill_by_snapshot(sid)
    assert not ok
    assert "expired" in msg


@pytest.mark.asyncio
async def test_snapshot_no_download_url(tmp_path):
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "", digest)
    ok, msg = await loader.install_skill_by_snapshot(sid)
    assert not ok
    assert "download_url" in msg


@pytest.mark.asyncio
async def test_snapshot_http_url_rejected(tmp_path):
    """http:// (non-TLS) URLs must be rejected."""
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "http://example.com/skill.zip", digest)
    ok, msg = await loader.install_skill_by_snapshot(sid)
    assert not ok
    assert "https" in msg


@pytest.mark.asyncio
async def test_snapshot_private_ip_rejected(tmp_path):
    """Private / SSRF addresses must be rejected."""
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "https://192.168.1.1/skill.zip", digest)
    ok, msg = await loader.install_skill_by_snapshot(sid)
    assert not ok
    assert "rejected" in msg or "private" in msg.lower() or "download_url" in msg


@pytest.mark.asyncio
async def test_snapshot_digest_mismatch(tmp_path):
    """A wrong digest must prevent installation."""
    loader = _make_loader(tmp_path)
    zip_bytes, _ = _build_skill_zip()
    sid = _register_snapshot(loader, "https://example.com/skill.zip", "a" * 64)

    mock_client = _make_httpx_mock(zip_bytes)
    with patch("httpx.AsyncClient", return_value=mock_client):
        ok, msg = await loader.install_skill_by_snapshot(sid)

    assert not ok
    assert "digest mismatch" in msg


@pytest.mark.asyncio
async def test_snapshot_http_error(tmp_path):
    """HTTP 4xx from download server must be reported as failure."""
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "https://example.com/skill.zip", digest)

    mock_client = _make_httpx_mock(b"", status=404)
    with patch("httpx.AsyncClient", return_value=mock_client):
        ok, msg = await loader.install_skill_by_snapshot(sid)

    assert not ok
    assert "404" in msg


@pytest.mark.asyncio
async def test_snapshot_replay_rejected(tmp_path):
    """After a successful (or attempted) install, the same snapshot_id must be rejected."""
    loader = _make_loader(tmp_path)
    _, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "https://example.com/skill.zip", digest)

    # First attempt consumes the snapshot (regardless of outcome)
    mock_client = _make_httpx_mock(b"", status=503)
    with patch("httpx.AsyncClient", return_value=mock_client):
        await loader.install_skill_by_snapshot(sid)

    # Second attempt must fail — snapshot is gone
    ok, msg = await loader.install_skill_by_snapshot(sid)
    assert not ok
    assert "invalid or expired" in msg


@pytest.mark.asyncio
async def test_snapshot_successful_install(tmp_path, monkeypatch):
    """
    Happy path: correct digest + valid zip raises AgentMidTurnReloadException,
    and the temp download file is cleaned up.
    """
    loader = _make_loader(tmp_path)
    zip_bytes, digest = _build_skill_zip()
    sid = _register_snapshot(loader, "https://example.com/skill.zip", digest)

    # Stub out the full sandbox install to avoid filesystem complexity
    monkeypatch.setattr(
        loader,
        "install_and_sanitize_skill_from_zip",
        lambda *a, **kw: (True, ""),
    )

    mock_client = _make_httpx_mock(zip_bytes)
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(AgentMidTurnReloadException) as exc_info:
            await loader.install_skill_by_snapshot(sid)

    assert "myskill" in str(exc_info.value)

    # Temp file must be cleaned up
    dl_dir = tmp_path / ".zen-claw" / "downloads"
    remaining = list(dl_dir.glob("_snapshot_*.zip"))
    assert remaining == [], f"temp zip not cleaned up: {remaining}"
