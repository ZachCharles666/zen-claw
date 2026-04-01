import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_dependency_versions(manifest: dict) -> list[tuple[str, str]]:
    versions: list[tuple[str, str]] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, version in manifest.get(section, {}).items():
            versions.append((str(name), str(version)))
    return versions


def test_root_repo_has_no_package_manifest() -> None:
    assert not (REPO_ROOT / "package.json").exists()


def test_all_node_subprojects_have_lockfiles() -> None:
    assert (REPO_ROOT / "bridge" / "package-lock.json").exists()
    assert (REPO_ROOT / "browser" / "sidecar" / "package-lock.json").exists()


def test_node_manifests_do_not_depend_on_axios_directly() -> None:
    manifests = (
        _load_json(REPO_ROOT / "bridge" / "package.json"),
        _load_json(REPO_ROOT / "browser" / "sidecar" / "package.json"),
    )
    direct_dependency_names = {
        name
        for manifest in manifests
        for name, _version in _iter_dependency_versions(manifest)
    }
    assert "axios" not in direct_dependency_names


def test_node_manifests_use_exact_direct_dependency_versions() -> None:
    manifests = (
        REPO_ROOT / "bridge" / "package.json",
        REPO_ROOT / "browser" / "sidecar" / "package.json",
    )
    for manifest_path in manifests:
        manifest = _load_json(manifest_path)
        for dependency_name, version in _iter_dependency_versions(manifest):
            assert not version.startswith("^"), (
                f"{manifest_path} keeps a wide version for {dependency_name}: {version}"
            )
