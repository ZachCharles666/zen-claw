"""Tests for multi-tenant architecture components."""

from __future__ import annotations

import uuid

import pytest


class TestTenantStore:
    @pytest.fixture
    def store(self, tmp_path):
        from zen_claw.auth.tenant import TenantStore

        return TenantStore(tmp_path)

    def test_create_tenant(self, store):
        t = store.create("Acme Corp")
        assert t.tenant_id
        assert t.name == "Acme Corp"
        assert t.enabled is True

    def test_get_existing_tenant(self, store):
        t = store.create("Test Org")
        fetched = store.get(t.tenant_id)
        assert fetched is not None
        assert fetched.name == "Test Org"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("00000000-0000-0000-0000-000000000000") is None

    def test_list_tenants(self, store):
        store.create("Org A")
        store.create("Org B")
        names = [t.name for t in store.list()]
        assert "Org A" in names and "Org B" in names

    def test_delete_disables_tenant(self, store):
        t = store.create("To Delete")
        assert store.delete(t.tenant_id) is True
        fetched = store.get(t.tenant_id)
        assert fetched is not None
        assert fetched.enabled is False

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete("00000000-0000-0000-0000-000000000000") is False

    def test_empty_name_raises(self, store):
        with pytest.raises(ValueError, match="cannot be empty"):
            store.create("")


class TestUserStore:
    @pytest.fixture
    def store(self, tmp_path):
        from zen_claw.auth.user import UserStore

        return UserStore(tmp_path)

    @pytest.fixture
    def tenant_id(self, tmp_path):
        from zen_claw.auth.tenant import TenantStore

        return TenantStore(tmp_path).create("Test Tenant").tenant_id

    def test_create_user(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        user = store.create(tenant_id, "alice", "securepassword123", role="admin")
        assert user.user_id
        assert user.username == "alice"
        assert user.role == "admin"
        assert "securepassword123" not in user.password_hash

    def test_authenticate_correct_password(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        store.create(tenant_id, "bob", "my_password_456", role="member")
        user = store.authenticate("bob", "my_password_456", tenant_id=tenant_id)
        assert user is not None
        assert user.username == "bob"

    def test_authenticate_wrong_password_returns_none(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        store.create(tenant_id, "carol", "correct_password", role="member")
        assert store.authenticate("carol", "wrong_password", tenant_id=tenant_id) is None

    def test_authenticate_unknown_user_returns_none(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        assert store.authenticate("nobody", "any_password", tenant_id=tenant_id) is None

    def test_duplicate_username_raises(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        store.create(tenant_id, "dave", "password123")
        with pytest.raises(ValueError, match="already exists"):
            store.create(tenant_id, "dave", "other_password")

    def test_short_password_raises(self, store, tenant_id):
        with pytest.raises(ValueError, match="8 characters"):
            store.create(tenant_id, "eve", "short")

    def test_list_by_tenant(self, store, tenant_id):
        pytest.importorskip("bcrypt")
        store.create(tenant_id, "user1", "password123")
        store.create(tenant_id, "user2", "password456")
        usernames = [u.username for u in store.list_by_tenant(tenant_id)]
        assert "user1" in usernames and "user2" in usernames


class TestSessionManager:
    SECRET = "test-secret-key-not-for-production-12345"

    @pytest.fixture
    def mgr(self):
        pytest.importorskip("jwt")
        from zen_claw.auth.session import SessionManager

        return SessionManager(self.SECRET, expire_seconds=300)

    def test_create_and_validate(self, mgr):
        token = mgr.create_session("uid-1", "tid-1", "alice", "admin")
        payload = mgr.validate_session(token)
        assert payload is not None
        assert payload["sub"] == "uid-1"
        assert payload["tid"] == "tid-1"
        assert payload["username"] == "alice"
        assert payload["role"] == "admin"

    def test_invalid_token_returns_none(self, mgr):
        assert mgr.validate_session("not.a.valid.jwt") is None

    def test_wrong_secret_returns_none(self, mgr):
        pytest.importorskip("jwt")
        from zen_claw.auth.session import SessionManager

        mgr2 = SessionManager("different-secret", expire_seconds=300)
        token = mgr.create_session("uid-2", "tid-2", "bob", "member")
        assert mgr2.validate_session(token) is None

    def test_expired_token_returns_none(self):
        pytest.importorskip("jwt")
        from zen_claw.auth.session import SessionManager

        mgr = SessionManager(self.SECRET, expire_seconds=-1)
        token = mgr.create_session("uid-3", "tid-3", "charlie", "member")
        assert mgr.validate_session(token) is None

    def test_empty_secret_raises(self):
        from zen_claw.auth.session import SessionManager

        with pytest.raises(ValueError, match="cannot be empty"):
            SessionManager("")


class TestTenantDataPaths:
    def test_valid_tenant_id(self, tmp_path):
        from zen_claw.auth.paths import tenant_data_dir

        tid = str(uuid.uuid4())
        p = tenant_data_dir(tmp_path, tid, "memory")
        assert str(tmp_path) in str(p)
        assert tid in str(p)
        assert "memory" in str(p)

    def test_default_tenant_id(self, tmp_path):
        from zen_claw.auth.paths import tenant_data_dir

        p = tenant_data_dir(tmp_path, "default")
        assert "default" in str(p)

    def test_invalid_tenant_id_raises(self, tmp_path):
        from zen_claw.auth.paths import tenant_data_dir

        with pytest.raises(ValueError):
            tenant_data_dir(tmp_path, "../../etc/passwd")

    def test_different_tenants_have_isolated_paths(self, tmp_path):
        from zen_claw.auth.paths import tenant_data_dir

        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        p1 = tenant_data_dir(tmp_path, t1, "data")
        p2 = tenant_data_dir(tmp_path, t2, "data")
        assert t1 in str(p1) and t1 not in str(p2)
        assert t2 in str(p2) and t2 not in str(p1)
