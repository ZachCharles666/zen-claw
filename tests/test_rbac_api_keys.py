"""Tests for A3 — RBAC API key management."""

from pathlib import Path

from zen_claw.auth.api_keys import (
    ApiKeyStore,
    generate_api_key,
    is_admin_path,
    is_write_path,
)

# ── generate_api_key ─────────────────────────────────────────────────────────

def test_generate_api_key_format() -> None:
    raw, prefix = generate_api_key()
    assert raw.startswith("nc-")
    assert prefix == raw[:12]
    assert len(raw) > 20


def test_generate_api_key_unique() -> None:
    raw1, _ = generate_api_key()
    raw2, _ = generate_api_key()
    assert raw1 != raw2


# ── ApiKeyStore: create / verify / role ─────────────────────────────────────

def test_store_create_and_verify(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, prefix = store.create(role="operator", label="test key")
    assert store.verify(raw) is True
    assert prefix == raw[:12]


def test_store_verify_unknown_key_returns_false(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    assert store.verify("nc-totally-fake-key") is False


def test_store_get_role_admin(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="admin")
    assert store.get_role(raw) == "admin"


def test_store_get_role_operator(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="operator")
    assert store.get_role(raw) == "operator"


def test_store_get_role_readonly(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="readonly")
    assert store.get_role(raw) == "readonly"


def test_store_get_role_invalid_key_returns_none(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    assert store.get_role("nc-nonexistent") is None


# ── has_role (hierarchy checks) ───────────────────────────────────────────────

def test_has_role_admin_can_do_admin(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="admin")
    assert store.has_role(raw, "admin") is True


def test_has_role_admin_can_do_operator(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="admin")
    assert store.has_role(raw, "operator") is True


def test_has_role_operator_cannot_do_admin(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="operator")
    assert store.has_role(raw, "admin") is False


def test_has_role_readonly_cannot_write(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, _ = store.create(role="readonly")
    assert store.has_role(raw, "operator") is False


# ── revoke + list ─────────────────────────────────────────────────────────────

def test_store_revoke_disables_key(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    raw, prefix = store.create(role="operator")
    assert store.verify(raw) is True

    found = store.revoke(prefix)
    assert found is True
    assert store.verify(raw) is False


def test_store_revoke_nonexistent_returns_false(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    assert store.revoke("nc-doesnotexist") is False


def test_store_list_keys(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path)
    store.create(role="admin", label="admin key")
    store.create(role="readonly", label="read key")

    keys = store.list_keys()
    assert len(keys) == 2
    roles = {k["role"] for k in keys}
    assert "admin" in roles
    assert "readonly" in roles
    # raw key never exposed
    for k in keys:
        assert "key" not in k


# ── Path helpers ──────────────────────────────────────────────────────────────

def test_is_admin_path_true() -> None:
    assert is_admin_path("/api/v1/admin/api-keys") is True
    assert is_admin_path("/api/v1/admin/anything") is True


def test_is_admin_path_false() -> None:
    assert is_admin_path("/api/v1/skills/list") is False
    assert is_admin_path("/api/v1/health") is False


def test_is_write_path_post_is_write() -> None:
    assert is_write_path("POST", "/api/v1/skills/install") is True
    assert is_write_path("DELETE", "/api/v1/agents/x") is True


def test_is_write_path_get_is_not_write() -> None:
    assert is_write_path("GET", "/api/v1/agents/list") is False
