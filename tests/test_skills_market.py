"""Tests for skills market registry and publisher."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from zen_claw.skills.publisher import SkillsPublisher
from zen_claw.skills.registry import RegistryEntry, SkillsRegistry

SAMPLE_CATALOG = {
    "version": "1",
    "updated_at": "2026-02-21T00:00:00Z",
    "skills": [
        {
            "name": "web-search",
            "version": "1.0.0",
            "description": "Search the web using multiple engines",
            "author": "zen-claw-team",
            "tags": ["web", "search"],
            "permissions": ["net.fetch", "net.search"],
            "enforce_ready": True,
            "size_bytes": 8192,
            "sha256": "abc123",
            "download_url": "https://example.com/web-search-1.0.0.zip",
            "homepage": "https://github.com/zen-claw/skills/web-search",
        },
        {
            "name": "pdf-reader",
            "version": "0.5.1",
            "description": "Extract text from PDF files",
            "author": "community",
            "tags": ["pdf", "document"],
            "permissions": ["fs.read"],
            "enforce_ready": True,
            "size_bytes": 4096,
            "sha256": "def456",
            "download_url": "https://example.com/pdf-reader-0.5.1.zip",
            "homepage": "",
        },
        {
            "name": "chat-helper",
            "version": "2.0.0",
            "description": "Enhanced chat formatting tools",
            "author": "zen-claw-team",
            "tags": ["chat", "formatting"],
            "permissions": [],
            "enforce_ready": False,
            "size_bytes": 2048,
            "sha256": "ghi789",
            "download_url": "https://example.com/chat-helper-2.0.0.zip",
            "homepage": "",
        },
    ],
}


@pytest.fixture
def local_registry(tmp_path: Path):
    catalog_path = tmp_path / "index.json"
    catalog_path.write_text(json.dumps(SAMPLE_CATALOG), encoding="utf-8")
    cache_path = tmp_path / "registry_cache.json"
    registry = SkillsRegistry(
        registry_url=f"file://{catalog_path}", cache_path=cache_path, cache_ttl_sec=3600
    )
    return registry, catalog_path


def test_registry_fetch_all(local_registry):
    registry, _ = local_registry
    entries = registry.fetch()
    assert len(entries) == 3
    names = [e.name for e in entries]
    assert "web-search" in names and "pdf-reader" in names and "chat-helper" in names


def test_registry_fetch_all_file_catalog_skips_trusted_time_network_probe(
    local_registry, monkeypatch
):
    registry, _ = local_registry

    def _unexpected_trusted_time_call():
        raise AssertionError("trusted time should not be queried for local file registry")

    monkeypatch.setattr(registry._trusted_time, "get_time", _unexpected_trusted_time_call)

    entries = registry.fetch()
    assert len(entries) == 3


def test_registry_search_by_query(local_registry):
    registry, _ = local_registry
    rows = registry.search(query="pdf")
    assert len(rows) == 1
    assert rows[0].name == "pdf-reader"


def test_registry_search_by_tag(local_registry):
    registry, _ = local_registry
    rows = registry.search(tag="web")
    assert len(rows) == 1 and rows[0].name == "web-search"


def test_registry_search_by_author(local_registry):
    registry, _ = local_registry
    rows = registry.search(author="zen-claw-team")
    names = [x.name for x in rows]
    assert len(rows) == 2 and "web-search" in names and "chat-helper" in names


def test_registry_search_enforce_ready_true(local_registry):
    registry, _ = local_registry
    rows = registry.search(enforce_ready=True)
    assert all(r.enforce_ready for r in rows)
    assert len(rows) == 2


def test_registry_search_enforce_ready_false(local_registry):
    registry, _ = local_registry
    rows = registry.search(enforce_ready=False)
    assert all(not r.enforce_ready for r in rows)
    assert len(rows) == 1 and rows[0].name == "chat-helper"


def test_registry_search_empty_query_returns_all(local_registry):
    registry, _ = local_registry
    assert len(registry.search()) == 3


def test_registry_caches_to_disk(local_registry, tmp_path: Path):
    registry, _ = local_registry
    cache = tmp_path / "registry_cache.json"
    assert not cache.exists()
    registry.fetch()
    assert cache.exists()
    cached = json.loads(cache.read_text(encoding="utf-8"))
    assert "hmac_sig" in cached and "payload" in cached
    payload = json.loads(cached["payload"])
    assert "skills" in payload and len(payload["skills"]) == 3


def test_registry_uses_cache_on_second_call(local_registry):
    registry, catalog_path = local_registry
    rows1 = registry.fetch()
    catalog_path.write_text('{"version":"1","skills":[]}', encoding="utf-8")
    rows2 = registry.fetch()
    assert len(rows1) == len(rows2) == 3


def test_registry_force_refresh_bypasses_cache(local_registry):
    registry, catalog_path = local_registry
    registry.fetch()
    new_catalog = {**SAMPLE_CATALOG, "skills": SAMPLE_CATALOG["skills"][:1]}
    catalog_path.write_text(json.dumps(new_catalog), encoding="utf-8")
    rows = registry.fetch(force=True)
    assert len(rows) == 1


def test_registry_missing_file_raises_runtime_error(tmp_path: Path):
    registry = SkillsRegistry(
        registry_url="file:///nonexistent/path/index.json",
        cache_path=tmp_path / "cache.json",
        cache_ttl_sec=0,
    )
    with pytest.raises(RuntimeError, match="Failed to read local catalog"):
        registry.fetch()


def test_registry_invalid_catalog_skips_bad_entries(tmp_path: Path):
    catalog = {
        "version": "1",
        "skills": [
            {"name": "", "version": "1.0", "description": ""},
            {"name": "good-skill", "version": "1.0", "description": "OK"},
        ],
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    registry = SkillsRegistry(
        registry_url=f"file://{path}", cache_path=tmp_path / "cache.json", cache_ttl_sec=0
    )
    rows = registry.fetch()
    assert len(rows) == 1 and rows[0].name == "good-skill"


def test_registry_entry_from_dict_and_to_dict():
    raw = SAMPLE_CATALOG["skills"][0]
    entry = RegistryEntry.from_dict(raw)
    out = entry.to_dict()
    assert out["name"] == raw["name"]
    assert out["version"] == raw["version"]
    assert out["sha256"] == raw["sha256"]
    assert out["tags"] == raw["tags"]


def _make_skill_dir(workspace: Path, name: str, manifest: dict | None = None) -> Path:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True)
    m = manifest or {
        "name": name,
        "version": "1.0.0",
        "description": f"Test skill {name}",
        "author": "test-author",
        "entry": "tools/main.py",
        "permissions": ["read_file"],
        "tags": ["test"],
    }
    (skill_dir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    tools_dir = skill_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "main.py").write_text("def get_tools(): return []\n", encoding="utf-8")
    return skill_dir


def _add_integrity(skill_dir: Path) -> None:
    import hashlib

    files: dict = {}
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file() and f.name != "integrity.json":
            files[str(f.relative_to(skill_dir))] = hashlib.sha256(f.read_bytes()).hexdigest()
    (skill_dir / "integrity.json").write_text(
        json.dumps({"files": files}, indent=2), encoding="utf-8"
    )


def test_publisher_skill_not_found(tmp_path: Path):
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("nonexistent-skill")
    assert result.ok is False
    assert "not found" in result.error.lower()


def test_publisher_missing_manifest(tmp_path: Path):
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("broken")
    assert result.ok is False
    assert "manifest.json" in result.error


def test_publisher_manifest_missing_required_keys(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "incomplete"
    skill_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(json.dumps({"name": "incomplete"}), encoding="utf-8")
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("incomplete")
    assert result.ok is False
    assert "missing required keys" in result.error


def test_publisher_success_without_integrity(tmp_path: Path):
    _make_skill_dir(tmp_path, "my-tool")
    out_dir = tmp_path / "dist"
    pub = SkillsPublisher(workspace=tmp_path, output_dir=out_dir, require_integrity=False)
    result = pub.publish("my-tool")
    assert result.ok is True
    assert result.sha256
    assert Path(result.zip_path).exists()
    assert Path(result.catalog_entry_path).exists()
    with zipfile.ZipFile(result.zip_path) as zf:
        assert any("manifest.json" in n for n in zf.namelist())
    catalog = json.loads(Path(result.catalog_entry_path).read_text(encoding="utf-8"))
    assert catalog["name"] == "my-tool"
    assert catalog["version"] == "1.0.0"
    assert catalog["sha256"] == result.sha256
    assert "download_url" in catalog


def test_publisher_catalog_preserves_runtime_contract(tmp_path: Path):
    _make_skill_dir(
        tmp_path,
        "weather-tool",
        {
            "name": "weather-tool",
            "version": "1.0.0",
            "description": "Weather tool",
            "author": "test-author",
            "entry": "tools/main.py",
            "permissions": ["web_fetch"],
            "runtime_contract": {
                "intent": "weather",
                "intent_mode": "router_first",
                "preferred_tools": ["web_fetch"],
                "allowed_tools": ["web_fetch"],
                "failure_mode": "runtime_direct",
            },
        },
    )
    pub = SkillsPublisher(workspace=tmp_path, output_dir=tmp_path / "dist", require_integrity=False)
    result = pub.publish("weather-tool")

    assert result.ok is True, result.error
    catalog = json.loads(Path(result.catalog_entry_path).read_text(encoding="utf-8"))
    assert catalog["runtime_contract"]["intent"] == "weather"
    assert catalog["runtime_contract"]["allowed_tools"] == ["web_fetch"]


def test_publisher_rejects_invalid_runtime_contract_manifest(tmp_path: Path):
    _make_skill_dir(
        tmp_path,
        "bad-weather",
        {
            "name": "bad-weather",
            "version": "1.0.0",
            "description": "Bad weather tool",
            "author": "test-author",
            "entry": "tools/main.py",
            "permissions": ["read_file"],
            "runtime_contract": {
                "intent": "weather",
                "preferred_tools": ["web_fetch"],
                "allowed_tools": ["web_fetch"],
            },
        },
    )
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("bad-weather")

    assert result.ok is False
    assert "Manifest validation failed" in result.error
    assert "allowed_tools must be a subset" in result.error


def test_publisher_integrity_check_fails_when_file_tampered(tmp_path: Path):
    skill_dir = _make_skill_dir(tmp_path, "tampered-skill")
    _add_integrity(skill_dir)
    (skill_dir / "tools" / "main.py").write_text(
        "def get_tools(): return ['x']\n", encoding="utf-8"
    )
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=True)
    result = pub.publish("tampered-skill")
    assert result.ok is False
    assert "Hash mismatch" in result.error or "Integrity check failed" in result.error


def test_publisher_success_with_integrity(tmp_path: Path):
    skill_dir = _make_skill_dir(tmp_path, "clean-skill")
    _add_integrity(skill_dir)
    pub = SkillsPublisher(workspace=tmp_path, output_dir=tmp_path / "dist", require_integrity=True)
    result = pub.publish("clean-skill")
    assert result.ok is True, result.error


def test_publisher_zip_excludes_symlinks(tmp_path: Path):
    skill_dir = _make_skill_dir(tmp_path, "symlink-skill")
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    symlink = skill_dir / "link_to_secret.txt"
    try:
        symlink.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("symlink-skill")
    assert result.ok is True
    with zipfile.ZipFile(result.zip_path) as zf:
        assert not any("link_to_secret" in n for n in zf.namelist())


def test_publisher_catalog_entry_sha256_matches_zip(tmp_path: Path):
    import hashlib

    _make_skill_dir(tmp_path, "sha-skill")
    pub = SkillsPublisher(workspace=tmp_path, require_integrity=False)
    result = pub.publish("sha-skill")
    actual = hashlib.sha256(Path(result.zip_path).read_bytes()).hexdigest()
    assert result.sha256 == actual
    catalog = json.loads(Path(result.catalog_entry_path).read_text(encoding="utf-8"))
    assert catalog["sha256"] == actual
