"""Dependabot's `uv` updates must never touch our own workspace members.

Unrelated PyPI packages share some member names (`scheduler`, `scaffolder`).
Dependabot then asks uv to pin the member to the PyPI version, which can never
resolve, and the whole `uv in /` job fails (run 38420183278). So the `uv` entry
ignores every member's project name, and a new member has to be added there too.
"""

from pathlib import Path
import tomllib

import yaml

REPO_ROOT = Path(__file__).parents[2]


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _member_names() -> set[str]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    names = set()
    for member in root["tool"]["uv"]["workspace"]["members"]:
        project = tomllib.loads((REPO_ROOT / member / "pyproject.toml").read_text())["project"]
        names.add(_canonical(project["name"]))
    return names


def _uv_ignored_names() -> set[str]:
    config = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text())
    (uv,) = [entry for entry in config["updates"] if entry["package-ecosystem"] == "uv"]
    return {_canonical(rule["dependency-name"]) for rule in uv.get("ignore", [])}


def test_every_workspace_member_is_ignored_by_the_uv_update():
    members = _member_names()
    assert members

    missing = members - _uv_ignored_names()

    assert not missing, (
        f"Workspace members {sorted(missing)} are missing from the `uv` entry's ignore "
        f"list in .github/dependabot.yml. A same-named PyPI package makes Dependabot "
        f"pin the member to it, which uv can never resolve."
    )
