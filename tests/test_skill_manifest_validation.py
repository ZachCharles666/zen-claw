"""Schema compliance tests for all built-in skill manifests.

Validates that every manifest.json under zen_claw/skills/ conforms to the
expected structure so that CI catches regressions when new skills are added.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

_SKILLS_DIR = Path(__file__).parent.parent / "zen_claw" / "skills"

_VALID_PERMISSIONS = {
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "exec",
    "web_fetch",
    "web_search",
    "message",
    "cron",
    "sessions",
    "knowledge",
    "identity",
    "credentials",
}

_VALID_SCOPES = {
    "filesystem",
    "network",
    "exec",
    "message",
    "cron",
    "sessions",
    "knowledge",
}

_VALID_TRUST = {"trusted", "untrusted"}

_VALID_RESPONSE_MODES = {
    "direct",
    "llm_assisted",
    "llm_only",
    "tool_chain",
}

_VALID_FAILURE_MODES = {
    "runtime_direct",
    "runtime_fact_llm_format",
    "llm_fallback",
    "guidance_only",
}


def _all_manifests() -> list[tuple[str, dict]]:
    """Return [(skill_name, manifest_dict), ...] for every built-in skill."""
    results = []
    for manifest_path in sorted(_SKILLS_DIR.glob("*/manifest.json")):
        skill_name = manifest_path.parent.name
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        results.append((skill_name, data))
    return results


# ── parametrized per-manifest tests ──────────────────────────────────────────

_MANIFESTS = _all_manifests()
_MANIFEST_IDS = [name for name, _ in _MANIFESTS]


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_has_required_string_fields(skill_name: str, manifest: dict) -> None:
    for field in ("name", "version", "description"):
        assert field in manifest, f"{skill_name}: missing '{field}'"
        assert isinstance(manifest[field], str), f"{skill_name}: '{field}' must be a string"
        assert manifest[field].strip(), f"{skill_name}: '{field}' must not be empty"


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_name_matches_directory(skill_name: str, manifest: dict) -> None:
    assert manifest["name"] == skill_name, (
        f"manifest name '{manifest['name']}' does not match directory '{skill_name}'"
    )


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_version_is_semver_like(skill_name: str, manifest: dict) -> None:
    import re
    version = manifest.get("version", "")
    assert re.match(r"^\d+\.\d+\.\d+", version), (
        f"{skill_name}: version '{version}' does not look like semver"
    )


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_permissions_are_valid(skill_name: str, manifest: dict) -> None:
    perms = manifest.get("permissions", [])
    assert isinstance(perms, list), f"{skill_name}: 'permissions' must be a list"
    unknown = set(perms) - _VALID_PERMISSIONS
    assert not unknown, (
        f"{skill_name}: unknown permission(s): {unknown}. "
        f"Valid: {sorted(_VALID_PERMISSIONS)}"
    )


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_scopes_are_valid(skill_name: str, manifest: dict) -> None:
    scopes = manifest.get("scopes", [])
    assert isinstance(scopes, list), f"{skill_name}: 'scopes' must be a list"
    unknown = set(scopes) - _VALID_SCOPES
    assert not unknown, (
        f"{skill_name}: unknown scope(s): {unknown}. "
        f"Valid: {sorted(_VALID_SCOPES)}"
    )


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_trust_field_is_valid_when_present(skill_name: str, manifest: dict) -> None:
    if "trust" not in manifest:
        return
    assert manifest["trust"] in _VALID_TRUST, (
        f"{skill_name}: trust must be one of {_VALID_TRUST}, got '{manifest['trust']}'"
    )


@pytest.mark.parametrize("skill_name,manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_manifest_runtime_contract_fields_are_valid_when_present(
    skill_name: str, manifest: dict
) -> None:
    contract = manifest.get("runtime_contract")
    if not contract:
        return
    assert isinstance(contract, dict), f"{skill_name}: 'runtime_contract' must be a dict"

    if "response_mode" in contract:
        assert contract["response_mode"] in _VALID_RESPONSE_MODES, (
            f"{skill_name}: invalid response_mode '{contract['response_mode']}'"
        )

    if "failure_mode" in contract:
        assert contract["failure_mode"] in _VALID_FAILURE_MODES, (
            f"{skill_name}: invalid failure_mode '{contract['failure_mode']}'"
        )

    for tool_field in ("preferred_tools", "allowed_tools"):
        if tool_field in contract:
            assert isinstance(contract[tool_field], list), (
                f"{skill_name}: '{tool_field}' must be a list"
            )


# ── coverage: all expected new skills are present ────────────────────────────

_EXPECTED_NEW_SKILLS = {
    "database_assistant",
    "browser_automation",
    "gdrive",
    "knowledge_management",
    "devops_monitor",
}

_EXPECTED_EXISTING_SKILLS = {
    "content_gen",
    "compliance_check",
    "rag_retrieve",
    "crawler",
    "github",
    "summarize",
    "weather",
    "cron",
    "tmux",
    "skill-creator",
}


def test_all_expected_new_skills_have_manifests() -> None:
    discovered = {name for name, _ in _MANIFESTS}
    missing = _EXPECTED_NEW_SKILLS - discovered
    assert not missing, f"Missing new skill manifests: {missing}"


def test_all_expected_existing_skills_have_manifests() -> None:
    discovered = {name for name, _ in _MANIFESTS}
    missing = _EXPECTED_EXISTING_SKILLS - discovered
    assert not missing, f"Missing existing skill manifests: {missing}"


def test_manifest_count_is_at_least_fifteen() -> None:
    """Guard against accidental deletion; project has 15+ built-in skills."""
    assert len(_MANIFESTS) >= 15, (
        f"Expected at least 15 skill manifests, found {len(_MANIFESTS)}"
    )


# ── SKILL.md presence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill_name,_manifest", _MANIFESTS, ids=_MANIFEST_IDS)
def test_skill_md_exists_alongside_manifest(skill_name: str, _manifest: dict) -> None:
    skill_md = _SKILLS_DIR / skill_name / "SKILL.md"
    assert skill_md.exists(), (
        f"{skill_name}: SKILL.md not found at {skill_md}"
    )
