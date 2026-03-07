"""Skills loader for agent capabilities."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from zen_claw.agent.intent_router import IntentToolContract
from zen_claw.errors import AgentMidTurnReloadException
from zen_claw.skills.registry import RegistryEntry, SkillsRegistry, TrustedTime
from zen_claw.utils.crypto import sign_data, verify_signature

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    _install_mutex = (
        asyncio.Lock()
    )  # Global cross-instance lock placeholder (real would use Redis/DB)
    _router_first_safe_tools = {"web_fetch", "message"}
    _high_risk_tools = {"exec", "spawn", "write_file", "edit_file"}

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self._state_file = workspace / ".zen-claw" / "skills_state.json"
        self._journal_file = workspace / ".zen-claw" / "install_journal.json"
        self._mapping_file = workspace / ".zen-claw" / "skill_mapping.json"
        self._allowed_permissions = {
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
            "exec",
            "web_search",
            "web_fetch",
            "message",
            "spawn",
            "cron",
            "sessions_spawn",
            "sessions_list",
            "sessions_kill",
            "sessions_read",
            "sessions_write",
            "sessions_signal",
            "sessions_resize",
        }
        self._allowed_scopes = {
            "network",
            "filesystem",
            "exec",
            "message",
            "cron",
            "sessions",
        }
        self._allowed_trust_levels = {"trusted", "untrusted"}
        self._zip_max_files = 100
        self._zip_max_path_depth = 10
        self._zip_max_total_uncompressed_bytes = 10 * 1024 * 1024
        self._install_allowed_roots = self._load_install_allowed_roots()
        self._skill_mapping: dict[str, list[str]] = {}
        self._snapshots: dict[str, dict] = {}  # snapshot_id -> metadata
        self.MAX_SNAPSHOT_AGE = 3600  # 1 hour TTL
        self._trusted_time = TrustedTime(self.workspace / ".zen-claw")
        self._gc_thread: threading.Thread | None = None
        self._gc_stop = threading.Event()
        self._load_skill_mapping()
        self._journal_recovery()

    async def search_skill(self, query: str) -> list[dict]:
        """
        Search for skills in the remote catalog and generate short-lived snapshots.

        Returns:
            List of skill metadata bits including a system-signed snapshot_id.
        """
        rows = self._search_registry(query=query.strip())
        now = self._now_ts()
        results: list[dict] = []
        for entry in rows:
            snapshot = {
                "name": entry.name,
                "version": entry.version,
                "digest": entry.sha256 or "",
                "publisher": entry.author or "unknown",
                "download_url": entry.download_url or "",
                "issued_at": now,
                "expires_at": now + self.MAX_SNAPSHOT_AGE,
                "nonce": os.urandom(8).hex(),
            }
            snapshot_id = self._sign_snapshot(snapshot)
            self._snapshots[snapshot_id] = snapshot
            results.append(
                {
                    "name": entry.name,
                    "version": entry.version,
                    "publisher": snapshot["publisher"],
                    "snapshot_id": snapshot_id,
                }
            )
        return results

    async def install_skill_by_snapshot(
        self, snapshot_id: str, overwrite: bool = False
    ) -> tuple[bool, str]:
        """Install a skill using a previously generated trusted snapshot."""
        async with self._install_mutex:
            snapshot = self._snapshots.get(snapshot_id)
            if not snapshot:
                return False, "invalid or expired snapshot_id"

            if self._sign_snapshot(snapshot) != snapshot_id:
                del self._snapshots[snapshot_id]
                return False, "invalid snapshot signature"

            if self._now_ts() > snapshot["expires_at"]:
                del self._snapshots[snapshot_id]
                return False, "snapshot expired"

            # Preventive cleanup snapshot after use (prevent replay)
            metadata = self._snapshots.pop(snapshot_id)
            name = metadata["name"]

            # Real impl would download and call self.install_skill_from_dir
            # For now, let's assume we got a new version physical dir: name_v2
            new_physical = f"{name}_v2"

            # Force reload exception to facilitate hot-swapping at turn boundary
            raise AgentMidTurnReloadException(
                f"Skill '{name}' installed. Reloading context.", pins={name: new_physical}
            )

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = self._discover_skills()

        # Filter by requirements and enabled state
        if filter_unavailable:
            return [
                s
                for s in skills
                if self._check_requirements(self._get_skill_meta(s["name"]))
                and self.is_skill_enabled(s["name"])
            ]
        return skills

    def is_skill_enabled(self, name: str) -> bool:
        """Check if a skill is enabled."""
        state = self._load_state()
        disabled = set(state.get("disabled", []))
        return name not in disabled

    def set_skill_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a skill. Returns False if skill does not exist."""
        all_names = {s["name"] for s in self.list_skills(filter_unavailable=False)}
        if name not in all_names:
            return False

        state = self._load_state()
        disabled = set(state.get("disabled", []))
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        state["disabled"] = sorted(disabled)
        self._save_state(state)
        return True

    def validate_skill_manifest(self, name: str, strict: bool = False) -> tuple[bool, list[str]]:
        """
        Validate one skill manifest.

        If strict=True, missing manifest is considered invalid.
        """
        skill = self._find_skill(name)
        if not skill:
            return False, [f"skill '{name}' not found"]

        manifest_path = skill["dir"] / "manifest.json"
        if not manifest_path.exists():
            return (False, ["manifest.json missing"]) if strict else (True, [])

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return False, [f"manifest json invalid: {str(e)}"]

        errors: list[str] = []
        if not isinstance(manifest, dict):
            return False, ["manifest root must be object"]

        for key in ("name", "version", "description"):
            if (
                key not in manifest
                or not isinstance(manifest[key], str)
                or not manifest[key].strip()
            ):
                errors.append(f"{key} must be non-empty string")

        if "name" in manifest and isinstance(manifest["name"], str):
            if manifest["name"] != name:
                errors.append("manifest name must match directory name")

        if "version" in manifest and isinstance(manifest["version"], str):
            if not re.match(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9]+)?$", manifest["version"]):
                errors.append("version should look like semver (e.g. 1.2.3)")

        if "permissions" in manifest:
            errors.extend(self._validate_permissions(manifest["permissions"]))
        elif strict:
            errors.append("permissions must be declared in strict mode")
        if "scopes" in manifest:
            errors.extend(self._validate_scopes(manifest["scopes"]))
        if "trust" in manifest:
            errors.extend(self._validate_trust(manifest["trust"]))
        errors.extend(self._validate_scope_permission_alignment(manifest))
        errors.extend(self._validate_runtime_contract(manifest))

        return len(errors) == 0, errors

    def validate_all_skill_manifests(self, strict: bool = False) -> list[dict[str, object]]:
        """Validate all skill manifests."""
        out: list[dict[str, object]] = []
        for s in self.list_skills(filter_unavailable=False):
            ok, errors = self.validate_skill_manifest(s["name"], strict=strict)
            out.append({"name": s["name"], "ok": ok, "errors": errors})
        return out

    def verify_skill_integrity(
        self, name: str, require_integrity: bool = False
    ) -> tuple[bool, list[str]]:
        """
        Verify a skill against manifest-declared file hashes.

        Expected manifest field:
        {
          "integrity": {
            "SKILL.md": "sha256:<hex>"  # or raw hex
          }
        }
        """
        skill = self._find_skill(name)
        if not skill:
            return False, [f"skill '{name}' not found"]
        root: Path = skill["dir"]  # type: ignore[assignment]

        manifest, load_errors = self.get_skill_manifest(name)
        if load_errors:
            return False, load_errors
        if not isinstance(manifest, dict):
            return False, ["manifest root must be object"]

        integrity = manifest.get("integrity")
        if integrity is None:
            return (
                (False, ["integrity missing in manifest.json"]) if require_integrity else (True, [])
            )
        if not isinstance(integrity, dict):
            return False, ["integrity must be an object mapping file path to hash"]

        errors: list[str] = []
        for rel_path, expected_value in integrity.items():
            rel = str(rel_path or "").strip()
            expected = str(expected_value or "").strip().lower()
            if not rel:
                errors.append("integrity contains empty file path")
                continue
            if expected.startswith("sha256:"):
                expected = expected.split(":", 1)[1].strip()
            if not re.match(r"^[a-f0-9]{64}$", expected):
                errors.append(f"integrity invalid sha256 for {rel}")
                continue
            target = (root / rel).resolve()
            if not target.is_relative_to(root.resolve()):
                errors.append(f"integrity path traversal blocked: {rel}")
                continue
            if not target.exists() or not target.is_file() or target.is_symlink():
                errors.append(f"integrity file missing: {rel}")
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"integrity mismatch: {rel}")

        return len(errors) == 0, errors

    def verify_all_skill_integrity(
        self, require_integrity: bool = False
    ) -> list[dict[str, object]]:
        """Verify integrity for all discovered skills."""
        out: list[dict[str, object]] = []
        for s in self.list_skills(filter_unavailable=False):
            ok, errors = self.verify_skill_integrity(s["name"], require_integrity=require_integrity)
            out.append({"name": s["name"], "ok": ok, "errors": errors})
        return out

    def get_skill_manifest_from_path(self, path: Path) -> tuple[dict | None, list[str]]:
        """Load a manifest from an arbitrary path."""
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            return None, ["manifest.json missing"]
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return data, [] if isinstance(data, dict) else ["manifest root must be object"]
        except Exception as e:
            return None, [str(e)]

    def get_skill_manifest(self, name: str) -> tuple[dict | None, list[str]]:
        """Load a skill manifest json object."""
        skill = self._find_skill(name)
        if not skill:
            return None, [f"skill '{name}' not found"]

        manifest_path = skill["dir"] / "manifest.json"
        if not manifest_path.exists():
            return None, ["manifest.json missing"]
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return None, [f"manifest json invalid: {str(e)}"]
        if not isinstance(data, dict):
            return None, ["manifest root must be object"]
        return data, []

    def get_skill_runtime_contract(self, name: str) -> tuple[IntentToolContract | None, list[str]]:
        """Load and validate runtime contract metadata from a skill manifest."""
        manifest, errors = self.get_skill_manifest(name)
        if errors:
            return None, errors
        return self.get_runtime_contract_from_manifest(manifest)

    def classify_skill_intent_mode(self, name: str) -> tuple[str | None, list[str]]:
        """Classify a skill as router_first / skill_first / hybrid from manifest metadata."""
        manifest, errors = self.get_skill_manifest(name)
        if errors:
            return None, errors
        return self.classify_runtime_contract_intent_mode(manifest)

    def classify_runtime_contract_intent_mode(
        self,
        manifest: dict | None,
    ) -> tuple[str | None, list[str]]:
        """Infer intent_mode when the tooling pipeline needs a stable classification."""
        if not isinstance(manifest, dict):
            return None, ["manifest root must be object"]
        payload = manifest.get("runtime_contract")
        if payload is None:
            return None, []
        if not isinstance(payload, dict):
            return None, ["runtime_contract must be object"]

        explicit_mode = str(payload.get("intent_mode") or "").strip().lower()
        if explicit_mode in {"router_first", "skill_first", "hybrid"}:
            return explicit_mode, []

        contract, errors = self.get_runtime_contract_from_manifest(manifest)
        if errors:
            return None, errors
        if contract is None:
            return "skill_first", []

        allowed_tools = set(contract.allowed_tools)
        if not allowed_tools:
            return "skill_first", []
        if bool(payload.get("allow_high_risk_escalation")):
            return "skill_first", []
        if allowed_tools & self._high_risk_tools:
            return "skill_first", []

        response_mode = str(payload.get("response_mode") or contract.response_mode).strip().lower()
        if (
            response_mode == "direct"
            and allowed_tools.issubset(self._router_first_safe_tools)
            and len(contract.preferred_tools) <= 2
        ):
            return "router_first", []
        return "hybrid", []

    @staticmethod
    def get_runtime_contract_from_manifest(
        manifest: dict | None,
    ) -> tuple[IntentToolContract | None, list[str]]:
        """Build a runtime contract from manifest metadata if present."""
        if not isinstance(manifest, dict):
            return None, ["manifest root must be object"]
        payload = manifest.get("runtime_contract")
        if payload is None:
            return None, []
        if not isinstance(payload, dict):
            return None, ["runtime_contract must be object"]
        contract = IntentToolContract.from_payload(payload)
        if contract is None:
            return None, ["runtime_contract invalid"]
        permissions = manifest.get("permissions")
        if isinstance(permissions, list):
            permission_set = {
                str(item).strip().lower() for item in permissions if str(item).strip()
            }
            if not contract.allowed_tools.issubset(permission_set):
                return None, ["runtime_contract allowed_tools must be a subset of manifest permissions"]
        return contract, []

    def load_skill(self, name: str, pin_dir: str | None = None) -> str | None:
        """
        Load a skill by name, optionally forcing a specific pinned physical directory.

        Args:
            name: Skill name (logical name).
            pin_dir: Optional physical directory name (e.g. 'my_skill_v1_0_0')

        Returns:
            Skill content or None if not found.
        """
        if pin_dir:
            # Try workspace first
            path = self.workspace_skills / pin_dir / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
            # Try built-in
            if self.builtin_skills:
                path = self.builtin_skills / pin_dir / "SKILL.md"
                if path.exists():
                    return path.read_text(encoding="utf-8")
            return None

        # Resolve via mapping or direct name (backward compatibility)
        resolved = self.resolve_physical_path(name)
        if resolved and (resolved / "SKILL.md").exists():
            return (resolved / "SKILL.md").read_text(encoding="utf-8")

        return None

    def load_skills_for_context(
        self, skill_names: list[str], pins: dict[str, str] | None = None
    ) -> str:
        """
        Load specific skills for inclusion in agent context, respecting pins.

        Args:
            skill_names: List of skill names to load.
            pins: Optional dict mapping logical names to physical dir names.

        Returns:
            Formatted skills content.
        """
        parts = []
        pins = pins or {}
        for name in skill_names:
            if not self.is_skill_enabled(name):
                continue
            content = self.load_skill(name, pin_dir=pins.get(name))
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_session_pins(self, active_skills: list[str]) -> dict[str, str]:
        """Resolve and pin current best versions for a list of logical skill names."""
        pins = {}
        for name in active_skills:
            resolved = self.resolve_physical_path(name)
            if resolved:
                pins[name] = resolved.name
        return pins

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f'  <skill available="{str(available).lower()}">')
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content

    def _parse_zen_claw_metadata(self, raw: str) -> dict:
        """Parse zen-claw metadata JSON from frontmatter."""
        try:
            data = json.loads(raw)
            return data.get("zen-claw", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get zen-claw metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_zen_claw_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_zen_claw_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip("\"'")
                return metadata

        return None

    def _load_skill_mapping(self) -> None:
        """Load the logical name -> physical versioned directories mapping."""
        if not self._mapping_file.exists():
            self._skill_mapping = {}
            return
        try:
            raw = self._mapping_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            # Verify HMAC
            if "sig" in data and "payload" in data:
                if verify_signature(data["payload"], data["sig"]):
                    self._skill_mapping = json.loads(data["payload"])
                    return
        except Exception:
            pass
        self._skill_mapping = {}

    def _save_skill_mapping(self) -> None:
        """Save the mapping with HMAC signature."""
        try:
            self._mapping_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._skill_mapping)
            sig = sign_data(payload)
            self._mapping_file.write_text(
                json.dumps({"payload": payload, "sig": sig}), encoding="utf-8"
            )
        except Exception:
            pass

    def _discover_skills(self) -> list[dict[str, str]]:
        """Discover workspace and built-in skills, resolving logical names."""
        skills: list[dict[str, str]] = []
        discovered_logical = set()

        def scan_dir(root: Path, source: str):
            if not root.exists():
                return
            for skill_dir in root.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue

                # Check manifest for name/version
                name = skill_dir.name
                version = "0.0.0"
                manifest_path = skill_dir / "manifest.json"
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        name = manifest.get("name", name)
                        version = manifest.get("version", version)
                    except Exception:
                        pass

                # If it's a versioned directory (physical), map it to logical name
                # For Phase 6, we assume physical directories are named like 'name_vVersion'
                # but we rely on manifest.name as the source of truth for logical name.
                if name not in self._skill_mapping:
                    self._skill_mapping[name] = []
                if skill_dir.name not in self._skill_mapping[name]:
                    self._skill_mapping[name].append(skill_dir.name)

                # Only expose the 'best' version for each logical name unless explicitly asked
                # Here we just list all unique logical names for the summary
                if name not in discovered_logical:
                    skills.append(
                        {
                            "name": name,
                            "path": str(skill_file),
                            "source": source,
                            "physical_dir": skill_dir.name,
                        }
                    )
                    discovered_logical.add(name)

        scan_dir(self.workspace_skills, "workspace")
        if self.builtin_skills:
            scan_dir(self.builtin_skills, "builtin")

        self._save_skill_mapping()
        return skills

    def resolve_physical_path(
        self, logical_name: str, version_pin: str | None = None
    ) -> Path | None:
        """Resolve a logical skill name to a physical directory path."""
        # Check mapping first
        physical_dirs = self._skill_mapping.get(logical_name, [])
        if not physical_dirs:
            # Fallback to direct name matching if not in mapping (e.g. built-in legacy)
            direct_ws = self.workspace_skills / logical_name
            if (direct_ws / "SKILL.md").exists():
                return direct_ws
            if self.builtin_skills:
                direct_bi = self.builtin_skills / logical_name
                if (direct_bi / "SKILL.md").exists():
                    return direct_bi
            return None

        # If version_pin is provided, try to find matching version in manifest
        # Otherwise, pick the latest semver version
        candidates = []
        for p_dir in physical_dirs:
            path = self.workspace_skills / p_dir
            if not path.exists():
                # Try built-in if workspace doesn't have it (shouldn't happen with our scan)
                if self.builtin_skills:
                    path = self.builtin_skills / p_dir

            if not path.exists():
                continue

            manifest_path = path / "manifest.json"
            if manifest_path.exists():
                # ... check version ...
                pass
            candidates.append(p_dir)

        if not candidates:
            return None

        # For now, just pick the last one (assuming sorted or highest)
        # TODO: Implement semver sort and pin logic
        best_dir = candidates[-1]
        ws_path = self.workspace_skills / best_dir
        if ws_path.exists():
            return ws_path
        if self.builtin_skills:
            return self.builtin_skills / best_dir
        return None

    def _find_skill(self, name: str) -> dict[str, object] | None:
        """Find skill directory and source for a skill name."""
        workspace_dir = self.workspace_skills / name
        if (workspace_dir / "SKILL.md").exists():
            return {"name": name, "dir": workspace_dir, "source": "workspace"}
        if self.builtin_skills:
            builtin_dir = self.builtin_skills / name
            if (builtin_dir / "SKILL.md").exists():
                return {"name": name, "dir": builtin_dir, "source": "builtin"}
        return None

    def install_skill_from_dir(
        self,
        source_dir: Path,
        name: str | None = None,
        overwrite: bool = False,
        require_manifest: bool = False,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """
        Install a skill into workspace from a local directory.

        The source directory must contain SKILL.md.
        """
        src = source_dir.resolve()
        if not src.exists() or not src.is_dir():
            return False, f"source directory not found: {source_dir}"
        if not self._is_install_source_allowed(src):
            return False, f"source path not allowed by install allowlist: {src}"

        skill_name = name.strip() if isinstance(name, str) and name.strip() else src.name
        if not self._is_valid_skill_name(skill_name):
            return False, f"invalid skill name: {skill_name}"

        skill_md = src / "SKILL.md"
        if not skill_md.exists():
            return False, "source skill directory must contain SKILL.md"
        if self._contains_symlink(src):
            return False, "source skill directory must not contain symlinks"

        manifest_path = src / "manifest.json"
        if require_manifest and not manifest_path.exists():
            return False, "manifest.json missing (required by strict mode)"
        if manifest_path.exists():
            ok, errors = self._validate_manifest_file(
                manifest_path,
                skill_name,
                strict=require_manifest,
            )
            if not ok:
                return False, "; ".join(errors)

        return self._install_skill_tree(
            src,
            skill_name=skill_name,
            overwrite=overwrite,
            dry_run=dry_run,
        )

    def install_and_sanitize_skill_from_dir(
        self,
        source_dir: Path,
        name: str | None = None,
        overwrite: bool = False,
        require_manifest: bool = True,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Install a local skill only after sandbox sanitization and revalidation."""
        src = source_dir.resolve()
        if not src.exists() or not src.is_dir():
            return False, f"source directory not found: {source_dir}"
        if not self._is_install_source_allowed(src):
            return False, f"source path not allowed by install allowlist: {src}"

        skill_name = name.strip() if isinstance(name, str) and name.strip() else src.name
        if not self._is_valid_skill_name(skill_name):
            return False, f"invalid skill name: {skill_name}"

        with tempfile.TemporaryDirectory(prefix="zen-claw-skill-sanitize-") as tmp:
            sandbox_root = Path(tmp) / skill_name
            try:
                shutil.copytree(src, sandbox_root)
            except Exception as exc:
                return False, f"sanitize staging failed: {exc}"
            ok, msg = self._sanitize_skill_dir(
                sandbox_root,
                skill_name=skill_name,
                require_manifest=require_manifest,
            )
            if not ok:
                return False, msg
            return self._install_skill_tree(
                sandbox_root,
                skill_name=skill_name,
                overwrite=overwrite,
                dry_run=dry_run,
            )

    def _install_skill_tree(
        self,
        src: Path,
        skill_name: str,
        overwrite: bool = False,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Copy a validated skill tree into the workspace."""
        dst = self.workspace_skills / skill_name
        # Optional versioned install layout (disabled by default for compatibility).
        manifest, _ = self.get_skill_manifest_from_path(src)
        enable_versioned_dirs = str(
            os.environ.get("ZEN_CLAW_ENABLE_VERSIONED_SKILL_DIRS", "")
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if enable_versioned_dirs and manifest and "version" in manifest:
            version_suffix = str(manifest["version"]).replace(".", "_")
            dst = self.workspace_skills / f"{skill_name}_v{version_suffix}"

        if dst.exists() and not overwrite:
            return False, f"skill already exists: {dst.name} (use --overwrite)"
        if dry_run:
            return True, f"dry-run ok: installable skill {skill_name}"

        # 5. Journaled Transaction
        self._journal_add({"name": skill_name, "path": str(dst), "action": "install"})
        try:
            self.workspace_skills.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=overwrite)
            # Ensure newly installed skill is enabled.
            self.set_skill_enabled(skill_name, True)
            self._discover_skills()  # Update mapping
            self._journal_remove(skill_name)
            return True, f"installed skill: {skill_name} in {dst.name}"
        except Exception as e:
            # Leave partial state for recovery? Or clean up?
            # Journal says recovery should delete partial dirs.
            return False, f"install failed: {e}"

    def install_skill_from_zip(
        self,
        zip_path: Path,
        name: str | None = None,
        overwrite: bool = False,
        require_manifest: bool = False,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Install a skill from a local .zip archive."""
        src = zip_path.resolve()
        if not src.exists() or not src.is_file():
            return False, f"zip file not found: {zip_path}"
        if src.suffix.lower() != ".zip":
            return False, "source file must be .zip"
        if not self._is_install_source_allowed(src):
            return False, f"source path not allowed by install allowlist: {src}"

        with tempfile.TemporaryDirectory(prefix="zen-claw-skill-zip-") as tmp:
            tmpdir = Path(tmp)
            try:
                with zipfile.ZipFile(src, "r") as zf:
                    ok, err = self._safe_extract_zip(zf, tmpdir)
                    if not ok:
                        return False, err
            except zipfile.BadZipFile:
                return False, "invalid zip archive"

            candidates = []
            for p in tmpdir.rglob("SKILL.md"):
                if p.is_file():
                    candidates.append(p.parent)
            if not candidates:
                return False, "zip archive must contain SKILL.md"
            unique_candidates = sorted(
                {str(p.resolve()): p for p in candidates}.values(), key=lambda p: len(p.parts)
            )
            if len(unique_candidates) != 1:
                return False, "zip archive must contain exactly one skill directory"
            skill_dir = unique_candidates[0]
            return self.install_skill_from_dir(
                skill_dir,
                name=name,
                overwrite=overwrite,
                require_manifest=require_manifest,
                dry_run=dry_run,
            )

    def install_and_sanitize_skill_from_zip(
        self,
        zip_path: Path,
        name: str | None = None,
        overwrite: bool = False,
        require_manifest: bool = True,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """Install a skill from zip only after sandbox sanitization and revalidation."""
        src = zip_path.resolve()
        if not src.exists() or not src.is_file():
            return False, f"zip file not found: {zip_path}"
        if src.suffix.lower() != ".zip":
            return False, "source file must be .zip"
        if not self._is_install_source_allowed(src):
            return False, f"source path not allowed by install allowlist: {src}"

        with tempfile.TemporaryDirectory(prefix="zen-claw-skill-zip-") as tmp:
            tmpdir = Path(tmp)
            try:
                with zipfile.ZipFile(src, "r") as zf:
                    ok, err = self._safe_extract_zip(zf, tmpdir)
                    if not ok:
                        return False, err
            except zipfile.BadZipFile:
                return False, "invalid zip archive"

            candidates = []
            for p in tmpdir.rglob("SKILL.md"):
                if p.is_file():
                    candidates.append(p.parent)
            if not candidates:
                return False, "zip archive must contain SKILL.md"
            unique_candidates = sorted(
                {str(p.resolve()): p for p in candidates}.values(), key=lambda p: len(p.parts)
            )
            if len(unique_candidates) != 1:
                return False, "zip archive must contain exactly one skill directory"
            skill_dir = unique_candidates[0]
            return self.install_and_sanitize_skill_from_dir(
                skill_dir,
                name=name,
                overwrite=overwrite,
                require_manifest=require_manifest,
                dry_run=dry_run,
            )

    def _safe_extract_zip(self, zf: zipfile.ZipFile, target_dir: Path) -> tuple[bool, str]:
        """Extract zip entries safely to avoid path traversal, bombs, and homoglyph attacks."""
        base = target_dir.resolve()
        validated_entries: list[tuple[zipfile.ZipInfo, Path]] = []
        total_uncompressed = 0
        file_count = 0
        for info in zf.infolist():
            ok, err, dst = self._validate_zip_entry(info, base)
            if not ok:
                return False, err
            if dst is None:
                continue
            if info.is_dir():
                validated_entries.append((info, dst))
                continue
            file_count += 1
            if file_count > self._zip_max_files:
                return False, f"zip archive contains too many files (max {self._zip_max_files})"
            total_uncompressed += max(0, int(info.file_size))
            if total_uncompressed > self._zip_max_total_uncompressed_bytes:
                return False, "zip archive is too large after extraction"
            validated_entries.append((info, dst))

        for info, dst in validated_entries:
            if info.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
        return True, ""

    def _validate_zip_entry(
        self,
        info: zipfile.ZipInfo,
        base: Path,
    ) -> tuple[bool, str, Path | None]:
        """Validate a zip entry path before any extraction occurs."""
        raw_name = unicodedata.normalize("NFC", info.filename.replace("\\", "/"))
        if not raw_name:
            return False, "invalid zip entry path: empty entry name", None
        if raw_name.startswith("/") or raw_name.startswith("../") or "/../" in raw_name:
            return False, f"invalid zip entry path: {info.filename}", None
        if len(Path(raw_name).parts) > self._zip_max_path_depth:
            return False, f"zip entry exceeds max path depth: {info.filename}", None

        dst = (base / Path(raw_name)).resolve()
        if not dst.is_relative_to(base):
            return False, f"invalid zip entry path: {info.filename}", None
        return True, "", dst

    def _sanitize_skill_dir(
        self,
        skill_dir: Path,
        skill_name: str,
        require_manifest: bool = True,
    ) -> tuple[bool, str]:
        """Rewrite a staged skill into a safer, runtime-consumable form."""
        if not skill_dir.exists() or not skill_dir.is_dir():
            return False, f"source directory not found: {skill_dir}"
        if self._contains_symlink(skill_dir):
            return False, "source skill directory must not contain symlinks"

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return False, "source skill directory must contain SKILL.md"

        manifest_path = skill_dir / "manifest.json"
        if require_manifest and not manifest_path.exists():
            return False, "manifest.json missing (required by strict mode)"
        if not manifest_path.exists():
            return True, "sanitized skill without manifest"

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"manifest json invalid: {str(exc)}"
        if not isinstance(manifest, dict):
            return False, "manifest root must be object"

        sanitized_manifest = self._sanitize_skill_manifest(manifest, skill_name)
        ok, errors = self._validate_manifest_file(
            manifest_path=self._write_sanitized_manifest(manifest_path, sanitized_manifest),
            skill_name=skill_name,
            strict=require_manifest,
        )
        if not ok:
            return False, "; ".join(errors)

        skill_md.write_text(
            self._sanitize_skill_markdown(skill_md.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        return True, f"sanitized skill: {skill_name}"

    def _write_sanitized_manifest(self, manifest_path: Path, manifest: dict) -> Path:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return manifest_path

    def _sanitize_skill_manifest(self, manifest: dict, skill_name: str) -> dict:
        """Repair manifest fields so sanitized output becomes installable and constrained."""
        sanitized = dict(manifest)
        sanitized["name"] = skill_name
        sanitized["trust"] = "untrusted"

        contract_payload = sanitized.get("runtime_contract")
        if isinstance(contract_payload, dict):
            allowed_tools = contract_payload.get("allowed_tools")
            if isinstance(allowed_tools, list):
                normalized_allowed = [
                    str(item).strip().lower() for item in allowed_tools if str(item).strip()
                ]
                if normalized_allowed:
                    unique_allowed = list(dict.fromkeys(normalized_allowed))
                    sanitized["permissions"] = unique_allowed
                    if "scopes" in sanitized:
                        sanitized["scopes"] = self._scopes_for_permissions(unique_allowed)
                    preferred_tools = contract_payload.get("preferred_tools")
                    if isinstance(preferred_tools, list):
                        contract_payload["preferred_tools"] = [
                            tool
                            for tool in preferred_tools
                            if str(tool).strip().lower() in set(unique_allowed)
                        ]
                    contract_payload["allowed_tools"] = unique_allowed
            intent_mode, errors = self.classify_runtime_contract_intent_mode(sanitized)
            if not errors and intent_mode:
                contract_payload["intent_mode"] = intent_mode
            sanitized["runtime_contract"] = contract_payload

        return sanitized

    def _sanitize_skill_markdown(self, content: str) -> str:
        """Drop raw shell/network execution guidance from staged SKILL.md content."""
        sanitized_lines: list[str] = []
        scrubbed_any = False
        for line in content.splitlines():
            lowered = line.lower()
            if "curl" in lowered or re.search(r"\b(exec|spawn)\b", lowered):
                scrubbed_any = True
                continue
            sanitized_lines.append(line)
        if scrubbed_any:
            sanitized_lines.append("")
            sanitized_lines.append("> Sanitized for zero-trust runtime: use native tools only.")
        return "\n".join(sanitized_lines).rstrip() + "\n"

    def _scopes_for_permissions(self, permissions: list[str]) -> list[str]:
        permission_to_scope = {
            "web_search": "network",
            "web_fetch": "network",
            "read_file": "filesystem",
            "write_file": "filesystem",
            "edit_file": "filesystem",
            "list_dir": "filesystem",
            "exec": "exec",
            "spawn": "exec",
            "message": "message",
            "cron": "cron",
            "sessions_spawn": "sessions",
            "sessions_list": "sessions",
            "sessions_kill": "sessions",
            "sessions_read": "sessions",
            "sessions_write": "sessions",
            "sessions_signal": "sessions",
            "sessions_resize": "sessions",
        }
        scopes: list[str] = []
        for permission in permissions:
            scope = permission_to_scope.get(permission)
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes

    def uninstall_skill(self, name: str) -> tuple[bool, str]:
        """Uninstall a workspace skill (built-in skills cannot be removed)."""
        if not self._is_valid_skill_name(name):
            return False, f"invalid skill name: {name}"

        dst = self.workspace_skills / name
        if not dst.exists():
            if self.builtin_skills and (self.builtin_skills / name / "SKILL.md").exists():
                return False, "cannot uninstall built-in skill"
            return False, f"skill not found in workspace: {name}"

        shutil.rmtree(dst)

        # Clean disabled-state residue to keep state file consistent.
        state = self._load_state()
        disabled = set(state.get("disabled", []))
        if name in disabled:
            disabled.remove(name)
            state["disabled"] = sorted(disabled)
            self._save_state(state)
        return True, f"uninstalled skill: {name}"

    def export_skill_to_zip(
        self, name: str, out_zip: Path, overwrite: bool = False
    ) -> tuple[bool, str]:
        """Export a skill directory as a zip archive."""
        if not self._is_valid_skill_name(name):
            return False, f"invalid skill name: {name}"
        skill = self._find_skill(name)
        if not skill:
            return False, f"skill '{name}' not found"

        out = out_zip.resolve()
        if out.exists() and not overwrite:
            return False, f"output zip already exists: {out} (use --overwrite)"
        out.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            root: Path = skill["dir"]  # type: ignore[assignment]
            for p in root.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    arcname = f"{name}/{p.relative_to(root).as_posix()}"
                    zf.write(p, arcname=arcname)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        return True, f"exported skill: {name} -> {out} (sha256={digest})"

    def build_skills_sbom(self) -> dict[str, object]:
        """Build a deterministic SBOM-style inventory for all discovered skills."""
        skills = sorted(self.list_skills(filter_unavailable=False), key=lambda item: item["name"])
        rows: list[dict[str, object]] = []
        for item in skills:
            name = item["name"]
            found = self._find_skill(name)
            if not found:
                continue
            root: Path = found["dir"]  # type: ignore[assignment]
            manifest, manifest_errors = self.get_skill_manifest(name)
            if manifest_errors and any("manifest.json missing" in e for e in manifest_errors):
                manifest_status = "missing"
            else:
                ok_manifest, _ = self.validate_skill_manifest(name, strict=True)
                manifest_status = "valid" if ok_manifest else "invalid"

            files: list[dict[str, object]] = []
            for rel in ("SKILL.md", "manifest.json"):
                path = root / rel
                if not path.exists() or not path.is_file() or path.is_symlink():
                    continue
                files.append(
                    {
                        "path": rel,
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )

            trust = ""
            permissions: list[str] = []
            scopes: list[str] = []
            if isinstance(manifest, dict):
                trust = str(manifest.get("trust") or "").strip().lower()
                raw_perms = manifest.get("permissions")
                raw_scopes = manifest.get("scopes")
                if isinstance(raw_perms, list):
                    permissions = [str(p).strip() for p in raw_perms if str(p).strip()]
                if isinstance(raw_scopes, list):
                    scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]

            rows.append(
                {
                    "name": name,
                    "source": item["source"],
                    "enabled": self.is_skill_enabled(name),
                    "manifest_status": manifest_status,
                    "trust": trust,
                    "permissions": permissions,
                    "scopes": scopes,
                    "files": files,
                }
            )

        return {
            "schema": "zen-claw.skills.sbom.v1",
            "skills_count": len(rows),
            "skills": rows,
        }

    def _journal_add(self, task: dict) -> None:
        """Add a pending installation task to the HMAC-secured journal."""
        try:
            journal = self._load_journal()
            journal.append({**task, "ts": time.time()})
            self._save_journal(journal)
        except Exception:
            pass

    def _journal_remove(self, skill_name: str) -> None:
        """Remove a completed or failed task from the journal."""
        try:
            journal = self._load_journal()
            journal = [t for t in journal if t.get("name") != skill_name]
            self._save_journal(journal)
        except Exception:
            pass

    def _load_journal(self) -> list:
        if not self._journal_file.exists():
            return []
        try:
            data = json.loads(self._journal_file.read_text(encoding="utf-8"))
            if verify_signature(data["payload"], data["sig"]):
                return json.loads(data["payload"])
        except Exception:
            pass
        return []

    def _save_journal(self, journal: list) -> None:
        try:
            payload = json.dumps(journal)
            sig = sign_data(payload)
            self._journal_file.write_text(
                json.dumps({"payload": payload, "sig": sig}), encoding="utf-8"
            )
        except Exception:
            pass

    def _journal_recovery(self) -> None:
        """Recover from interrupted installs by deleting partial directories."""
        journal = self._load_journal()
        if not journal:
            return
        logger.info(f"Recovering from {len(journal)} interrupted installs...")
        for task in journal:
            path_str = task.get("path")
            if path_str:
                path = Path(path_str)
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
        self._save_journal([])

    def gc_cleanup(self, retention_hours: int = 24) -> int:
        """
        Clean up old versioned skill directories that are no longer pinned by any session.

        Args:
            retention_hours: Number of hours to keep unreferenced versions.

        Returns:
            Number of directories deleted.
        """
        # 1. Collect all active pins from session files
        pins = set()
        sessions_dir = Path.home() / ".zen-claw" / "sessions"
        if sessions_dir.exists():
            for path in sessions_dir.glob("*.jsonl"):
                try:
                    with open(path, encoding="utf-8") as f:
                        # Metadata is usually on the first or second line
                        for _ in range(5):  # Check first few lines for metadata
                            line = f.readline()
                            if not line:
                                break
                            data = json.loads(line)
                            if data.get("_type") == "metadata":
                                meta = data.get("metadata", {})
                                for p in meta.get("skill_pins", {}).values():
                                    pins.add(p)
                                break
                except Exception:
                    continue

        # 2. Collect current mappings (protect latest)
        for physical_dirs in self._skill_mapping.values():
            for physical in physical_dirs:
                pins.add(physical)

        # 3. Identify versioned directories and candidate for deletion
        deleted_count = 0
        cutoff = datetime.now().timestamp() - (retention_hours * 3600)

        for item in self.workspace_skills.iterdir():
            if not item.is_dir():
                continue
            if item.name in pins:
                continue
            # Basic versioning check (e.g. has _v in name)
            if "_v" not in item.name:
                continue

            # Check age
            try:
                if item.stat().st_mtime < cutoff:
                    shutil.rmtree(item, ignore_errors=True)
                    deleted_count += 1
            except OSError:
                continue

        return deleted_count

    def start_gc_reaper(self, *, interval_seconds: int = 3600, retention_hours: int = 24) -> None:
        """Start background GC reaper for stale unpinned skill versions."""
        interval_seconds = max(30, int(interval_seconds))
        if self._gc_thread and self._gc_thread.is_alive():
            return
        self._gc_stop.clear()

        def _worker() -> None:
            while not self._gc_stop.wait(interval_seconds):
                try:
                    deleted = self.gc_cleanup(retention_hours=retention_hours)
                    if deleted:
                        logger.info(
                            f"Skill GC reaper deleted {deleted} stale versioned directories"
                        )
                except Exception as exc:
                    logger.warning(f"Skill GC reaper error: {exc}")

        self._gc_thread = threading.Thread(
            target=_worker,
            name="skills-gc-reaper",
            daemon=True,
        )
        self._gc_thread.start()

    def stop_gc_reaper(self, timeout_seconds: float = 2.0) -> None:
        """Stop background GC reaper if running."""
        self._gc_stop.set()
        if self._gc_thread and self._gc_thread.is_alive():
            self._gc_thread.join(timeout=timeout_seconds)

    def _now_ts(self) -> float:
        """Current trusted timestamp with safe fallback."""
        try:
            return float(self._trusted_time.get_time())
        except Exception:
            return datetime.now().timestamp()

    @staticmethod
    def _sign_snapshot(snapshot: dict[str, Any]) -> str:
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sign_data(payload)

    def _search_registry(self, query: str) -> list[RegistryEntry]:
        registry_url, cache_file, cache_ttl, trusted_hosts = self._resolve_runtime_market_config()
        cache_path = self.workspace / ".zen-claw" / "skills" / cache_file
        registry = SkillsRegistry(
            registry_url=registry_url,
            cache_path=cache_path,
            cache_ttl_sec=cache_ttl,
            trusted_hosts=trusted_hosts,
        )
        try:
            return registry.search(query=query, force_refresh=False)
        except RuntimeError as exc:
            logger.warning(f"Skill registry search failed: {exc}")
            return []

    def _resolve_runtime_market_config(self) -> tuple[str, str, int, list[str]]:
        """Load runtime market config, fallback to safe defaults."""
        registry_url = (
            os.environ.get("ZEN_CLAW_SKILLS_REGISTRY_URL", "").strip()
            or "https://zen-claw.github.io/skills-registry/index.json"
        )
        cache_file = "registry_cache.json"
        cache_ttl = 3600
        trusted_hosts: list[str] = []
        trusted_hosts_env = os.environ.get("ZEN_CLAW_SKILLS_TRUSTED_HOSTS", "").strip()
        if trusted_hosts_env:
            trusted_hosts = [h.strip() for h in trusted_hosts_env.split(",") if h.strip()]
        try:
            from zen_claw.config.loader import load_config

            cfg = load_config()
            market = getattr(cfg, "skills_market", None)
            if market is not None:
                registry_url = str(getattr(market, "registry_url", registry_url) or registry_url)
                cache_file = str(getattr(market, "cache_file", cache_file) or cache_file)
                cache_ttl = int(getattr(market, "cache_ttl_sec", cache_ttl) or cache_ttl)
                cfg_hosts = getattr(market, "trusted_hosts", None)
                if isinstance(cfg_hosts, list) and cfg_hosts:
                    trusted_hosts = [str(h).strip() for h in cfg_hosts if str(h).strip()]
        except Exception:
            pass
        return registry_url, cache_file, max(0, cache_ttl), trusted_hosts

    def _load_state(self) -> dict:
        """Load skill state file."""
        if not self._state_file.exists():
            return {"disabled": []}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"disabled": []}

    def _save_state(self, state: dict) -> None:
        """Save skill state file."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _is_valid_skill_name(self, name: str) -> bool:
        """Allow only safe skill directory names."""
        return bool(re.match(r"^[A-Za-z0-9._-]+$", name))

    def _load_install_allowed_roots(self) -> list[Path]:
        """
        Parse optional install source allowlist from env.

        Env:
        - zen_claw_SKILL_INSTALL_ALLOWED_ROOTS
          Semicolon-separated absolute paths.
          Empty means unrestricted (backward compatible).
        """
        raw = str(os.environ.get("zen_claw_SKILL_INSTALL_ALLOWED_ROOTS", "")).strip()
        if not raw:
            return []
        roots: list[Path] = []
        seen: set[str] = set()
        for token in raw.split(";"):
            p = token.strip()
            if not p:
                continue
            try:
                root = Path(p).resolve()
            except OSError:
                continue
            if not root.is_absolute():
                continue
            key = str(root).lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        return roots

    def _is_install_source_allowed(self, src: Path) -> bool:
        """Check whether source path is within configured allowlist roots."""
        if not self._install_allowed_roots:
            return True
        resolved = src.resolve()
        for root in self._install_allowed_roots:
            try:
                if resolved == root or resolved.is_relative_to(root):
                    return True
            except Exception:
                continue
        return False

    def _contains_symlink(self, root: Path) -> bool:
        """Detect symlinks in source tree."""
        try:
            for p in root.rglob("*"):
                if p.is_symlink():
                    return True
        except OSError:
            return True
        return False

    def _validate_manifest_file(
        self,
        manifest_path: Path,
        skill_name: str,
        strict: bool = False,
    ) -> tuple[bool, list[str]]:
        """Validate a manifest file for install preflight."""
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return False, [f"manifest json invalid: {str(e)}"]

        errors: list[str] = []
        if not isinstance(manifest, dict):
            return False, ["manifest root must be object"]

        for key in ("name", "version", "description"):
            if (
                key not in manifest
                or not isinstance(manifest[key], str)
                or not manifest[key].strip()
            ):
                errors.append(f"{key} must be non-empty string")

        if "name" in manifest and isinstance(manifest["name"], str):
            if manifest["name"] != skill_name:
                errors.append("manifest name must match directory name")

        if "version" in manifest and isinstance(manifest["version"], str):
            if not re.match(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9]+)?$", manifest["version"]):
                errors.append("version should look like semver (e.g. 1.2.3)")

        if "permissions" in manifest:
            errors.extend(self._validate_permissions(manifest["permissions"]))
        elif strict:
            errors.append("permissions must be declared in strict mode")
        if "scopes" in manifest:
            errors.extend(self._validate_scopes(manifest["scopes"]))
        if "trust" in manifest:
            errors.extend(self._validate_trust(manifest["trust"]))
        errors.extend(self._validate_scope_permission_alignment(manifest))
        errors.extend(self._validate_runtime_contract(manifest))
        return len(errors) == 0, errors

    def _validate_runtime_contract(self, manifest: dict) -> list[str]:
        """Validate optional runtime_contract metadata in skill manifest."""
        payload = manifest.get("runtime_contract")
        if payload is None:
            return []
        if not isinstance(payload, dict):
            return ["runtime_contract must be object"]

        errors: list[str] = []
        intent = payload.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            errors.append("runtime_contract.intent must be non-empty string")

        intent_mode = payload.get("intent_mode")
        if intent_mode is not None:
            if not isinstance(intent_mode, str) or intent_mode.strip().lower() not in {
                "router_first",
                "skill_first",
                "hybrid",
            }:
                errors.append(
                    "runtime_contract.intent_mode must be one of ['router_first', 'skill_first', 'hybrid']"
                )

        response_mode = payload.get("response_mode")
        if response_mode is not None:
            if not isinstance(response_mode, str) or response_mode.strip().lower() not in {
                "direct",
                "llm_assisted",
            }:
                errors.append(
                    "runtime_contract.response_mode must be one of ['direct', 'llm_assisted']"
                )

        failure_mode = payload.get("failure_mode")
        if failure_mode is not None:
            if not isinstance(failure_mode, str) or failure_mode.strip().lower() not in {
                "runtime_direct",
                "runtime_fact_llm_format",
            }:
                errors.append(
                    "runtime_contract.failure_mode must be one of ['runtime_direct', 'runtime_fact_llm_format']"
                )

        for field in ("preferred_tools", "allowed_tools", "denied_tools"):
            value = payload.get(field)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"runtime_contract.{field} must be list[str]")
                continue
            normalized = [item.strip().lower() for item in value]
            if any(not item for item in normalized):
                errors.append(f"runtime_contract.{field} entries must be non-empty strings")
            invalid = sorted({item for item in normalized if item and item not in self._allowed_permissions})
            if invalid:
                errors.append(f"runtime_contract.{field} contains unknown tools: {invalid}")

        allowed_tools = payload.get("allowed_tools")
        if isinstance(allowed_tools, list) and all(isinstance(item, str) for item in allowed_tools):
            allowed_set = {item.strip().lower() for item in allowed_tools if item.strip()}
            preferred_tools = payload.get("preferred_tools")
            if isinstance(preferred_tools, list) and all(isinstance(item, str) for item in preferred_tools):
                preferred_set = {item.strip().lower() for item in preferred_tools if item.strip()}
                if not preferred_set.issubset(allowed_set):
                    errors.append(
                        "runtime_contract.preferred_tools must be a subset of runtime_contract.allowed_tools"
                    )
            permissions = manifest.get("permissions")
            if isinstance(permissions, list) and all(isinstance(item, str) for item in permissions):
                permission_set = {item.strip().lower() for item in permissions if item.strip()}
                if not allowed_set.issubset(permission_set):
                    errors.append(
                        "runtime_contract.allowed_tools must be a subset of manifest permissions"
                    )

        fact_payload_schema = payload.get("fact_payload_schema")
        if fact_payload_schema is not None and not isinstance(fact_payload_schema, dict):
            errors.append("runtime_contract.fact_payload_schema must be object")

        return errors

    def _validate_permissions(self, permissions: object) -> list[str]:
        """Validate permissions field in skill manifest."""
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            return ["permissions must be list[str]"]

        errors: list[str] = []
        normalized = [p.strip() for p in permissions]
        invalid_empty = [p for p in normalized if not p]
        if invalid_empty:
            errors.append("permissions entries must be non-empty strings")

        invalid = sorted({p for p in normalized if p and p not in self._allowed_permissions})
        if invalid:
            errors.append(f"permissions contains unknown entries: {invalid}")

        seen: set[str] = set()
        dupes: set[str] = set()
        for p in normalized:
            if not p:
                continue
            if p in seen:
                dupes.add(p)
            seen.add(p)
        duplicates = sorted(dupes)
        if duplicates:
            errors.append(f"permissions contains duplicate entries: {duplicates}")
        return errors

    def _validate_scopes(self, scopes: object) -> list[str]:
        """Validate optional scopes field in skill manifest."""
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            return ["scopes must be list[str]"]

        errors: list[str] = []
        normalized = [s.strip().lower() for s in scopes]
        invalid_empty = [s for s in normalized if not s]
        if invalid_empty:
            errors.append("scopes entries must be non-empty strings")

        invalid = sorted({s for s in normalized if s and s not in self._allowed_scopes})
        if invalid:
            errors.append(f"scopes contains unknown entries: {invalid}")

        seen: set[str] = set()
        dupes: set[str] = set()
        for s in normalized:
            if not s:
                continue
            if s in seen:
                dupes.add(s)
            seen.add(s)
        duplicates = sorted(dupes)
        if duplicates:
            errors.append(f"scopes contains duplicate entries: {duplicates}")
        return errors

    def _validate_scope_permission_alignment(self, manifest: dict) -> list[str]:
        """Ensure declared permissions are covered by declared scopes when scopes are present."""
        scopes = manifest.get("scopes")
        perms = manifest.get("permissions")
        if not isinstance(scopes, list) or not isinstance(perms, list):
            return []
        if not all(isinstance(s, str) and s.strip() for s in scopes):
            return []
        if not all(isinstance(p, str) and p.strip() for p in perms):
            return []

        scope_to_permissions = {
            "network": {"web_search", "web_fetch"},
            "filesystem": {"read_file", "write_file", "edit_file", "list_dir"},
            "exec": {"exec", "spawn"},
            "message": {"message"},
            "cron": {"cron"},
            "sessions": {
                "sessions_spawn",
                "sessions_list",
                "sessions_kill",
                "sessions_read",
                "sessions_write",
                "sessions_signal",
                "sessions_resize",
            },
        }
        declared_scopes = {s.strip().lower() for s in scopes if s.strip()}
        declared_permissions = {p.strip().lower() for p in perms if p.strip()}
        covered: set[str] = set()
        for s in declared_scopes:
            covered |= scope_to_permissions.get(s, set())
        uncovered = sorted({p for p in declared_permissions if p not in covered})
        if uncovered:
            return [f"permissions not covered by scopes: {uncovered}"]
        return []

    def _validate_trust(self, trust: object) -> list[str]:
        """Validate optional trust field in skill manifest."""
        if not isinstance(trust, str):
            return ["trust must be string ('trusted'|'untrusted')"]
        token = trust.strip().lower()
        if token not in self._allowed_trust_levels:
            return [f"trust must be one of: {sorted(self._allowed_trust_levels)}"]
        return []
