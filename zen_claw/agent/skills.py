"""Skills loader for agent capabilities."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.
    
    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self._state_file = workspace / ".zen-claw" / "skills_state.json"
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
        self._zip_max_files = 200
        self._zip_max_total_uncompressed_bytes = 10 * 1024 * 1024
        self._install_allowed_roots = self._load_install_allowed_roots()

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
                if self._check_requirements(self._get_skill_meta(s["name"])) and self.is_skill_enabled(s["name"])
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
            if key not in manifest or not isinstance(manifest[key], str) or not manifest[key].strip():
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

        return len(errors) == 0, errors

    def validate_all_skill_manifests(self, strict: bool = False) -> list[dict[str, object]]:
        """Validate all skill manifests."""
        out: list[dict[str, object]] = []
        for s in self.list_skills(filter_unavailable=False):
            ok, errors = self.validate_skill_manifest(s["name"], strict=strict)
            out.append({"name": s["name"], "ok": ok, "errors": errors})
        return out

    def verify_skill_integrity(self, name: str, require_integrity: bool = False) -> tuple[bool, list[str]]:
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
            return (False, ["integrity missing in manifest.json"]) if require_integrity else (True, [])
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

    def verify_all_skill_integrity(self, require_integrity: bool = False) -> list[dict[str, object]]:
        """Verify integrity for all discovered skills."""
        out: list[dict[str, object]] = []
        for s in self.list_skills(filter_unavailable=False):
            ok, errors = self.verify_skill_integrity(s["name"], require_integrity=require_integrity)
            out.append({"name": s["name"], "ok": ok, "errors": errors})
        return out

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

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.
        
        Args:
            name: Skill name (directory name).
        
        Returns:
            Skill content or None if not found.
        """
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.
        
        Args:
            skill_names: List of skill names to load.
        
        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            if not self.is_skill_enabled(name):
                continue
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

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

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
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
                return content[match.end():].strip()
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
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None

    def _discover_skills(self) -> list[dict[str, str]]:
        """Discover workspace and built-in skills with precedence rules."""
        skills: list[dict[str, str]] = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        return skills

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

        dst = self.workspace_skills / skill_name
        if dst.exists() and not overwrite:
            return False, f"skill already exists: {skill_name} (use --overwrite)"
        if dry_run:
            return True, f"dry-run ok: installable skill {skill_name}"

        self.workspace_skills.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=overwrite)

        # Ensure newly installed skill is enabled.
        self.set_skill_enabled(skill_name, True)
        return True, f"installed skill: {skill_name}"

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
            unique_candidates = sorted({str(p.resolve()): p for p in candidates}.values(), key=lambda p: len(p.parts))
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

    def _safe_extract_zip(self, zf: zipfile.ZipFile, target_dir: Path) -> tuple[bool, str]:
        """Extract zip entries safely to avoid path traversal."""
        base = target_dir.resolve()
        total_uncompressed = 0
        file_count = 0
        for info in zf.infolist():
            raw_name = info.filename.replace("\\", "/")
            if raw_name.startswith("/") or raw_name.startswith("../") or "/../" in raw_name:
                return False, f"invalid zip entry path: {info.filename}"
            dst = (base / Path(raw_name)).resolve()
            if not dst.is_relative_to(base):
                return False, f"invalid zip entry path: {info.filename}"
            if info.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            file_count += 1
            if file_count > self._zip_max_files:
                return False, "zip archive contains too many files"
            total_uncompressed += max(0, int(info.file_size))
            if total_uncompressed > self._zip_max_total_uncompressed_bytes:
                return False, "zip archive is too large after extraction"
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
        return True, ""

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

    def export_skill_to_zip(self, name: str, out_zip: Path, overwrite: bool = False) -> tuple[bool, str]:
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
            if key not in manifest or not isinstance(manifest[key], str) or not manifest[key].strip():
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
        return len(errors) == 0, errors

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


