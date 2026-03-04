import json
import os
from pathlib import Path

import pytest

try:
    from cryptography.fernet import Fernet

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _CRYPTO_AVAILABLE, reason="cryptography not installed")

from zen_claw.auth.credentials import CredentialVault


def _make_vault(tmp_path: Path, key: str | None = None) -> CredentialVault:
    if key is None:
        key = Fernet.generate_key().decode("ascii")
    os.environ["zen_claw_VAULT_KEY"] = key
    return CredentialVault(data_dir=tmp_path)


def test_store_get_round_trip(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("github", "api_key", "ghp_secret")
    assert v.get("github", "api_key") == "ghp_secret"


def test_stored_file_does_not_contain_plaintext(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("twitter", "access_token", "plaintext_value_123")
    p = tmp_path / "credentials" / "twitter.json"
    raw = p.read_text(encoding="utf-8")
    assert "plaintext_value_123" not in raw
    data = json.loads(raw)
    assert "access_token" in data


def test_wrong_key_returns_none(tmp_path: Path) -> None:
    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")
    _make_vault(tmp_path, key_a).store("github", "token", "secret-a")
    assert _make_vault(tmp_path, key_b).get("github", "token") is None


def test_list_platforms(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("twitter", "t", "1")
    v.store("github", "k", "2")
    v.store("openai", "k", "3")
    assert v.list_platforms() == ["github", "openai", "twitter"]


def test_list_keys(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("github", "access_token", "a")
    v.store("github", "refresh_token", "b")
    assert v.list_keys("github") == ["access_token", "refresh_token"]


def test_delete_return_values(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("slack", "bot", "x")
    assert v.delete("slack", "bot") is True
    assert v.delete("slack", "bot") is False
    assert v.delete("none", "x") is False


def test_empty_vault_platforms_empty(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    assert v.list_platforms() == []


def test_multiple_keys_per_platform(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("twitter", "a", "1")
    v.store("twitter", "b", "2")
    v.store("twitter", "c", "3")
    assert v.get("twitter", "a") == "1"
    assert v.get("twitter", "b") == "2"
    assert v.get("twitter", "c") == "3"


def test_delete_makes_key_unretrievable(tmp_path: Path) -> None:
    v = _make_vault(tmp_path)
    v.store("openai", "api_key", "sk-1")
    assert v.get("openai", "api_key") == "sk-1"
    assert v.delete("openai", "api_key") is True
    assert v.get("openai", "api_key") is None


def test_auto_generated_key_file_reusable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("zen_claw_VAULT_KEY", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    v1 = CredentialVault(data_dir=tmp_path / "data")
    v1.store("p", "k", "v")
    v2 = CredentialVault(data_dir=tmp_path / "data")
    assert v2.get("p", "k") == "v"

