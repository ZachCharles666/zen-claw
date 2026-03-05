import tomllib
from pathlib import Path

import zen_claw


def test_package_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert zen_claw.__version__ == data["project"]["version"]
