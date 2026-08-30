"""Every name in the root `[tool.uv.sources]` must be depended on by something.

`[tool.uv.sources]` only redirects where a requirement resolves from; it does not
create a requirement. An entry nobody depends on installs nothing and is invisible
in `uv.lock`, but reads like a declared package boundary. Three such entries for
`shared` is imported from the repository tree rather than installed as a workspace member.

Referrers are collected from the root pyproject (project dependencies and every
dependency group) and from every workspace member's pyproject, so a source entry
survives only while some real requirement points at it.
"""

from pathlib import Path
import re
import tomllib

import pytest

REPO_ROOT = Path(__file__).parents[2]
ROOT_PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def _canonical(requirement: str) -> str:
    """`pyjwt[crypto]>=2.8.0` -> `pyjwt`, `Foo_Bar` -> `foo-bar`."""
    name = re.split(r"[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-").replace(".", "-")


def _requirements(pyproject: dict) -> set[str]:
    names = {_canonical(dep) for dep in pyproject.get("project", {}).get("dependencies", [])}
    for extra in pyproject.get("project", {}).get("optional-dependencies", {}).values():
        names |= {_canonical(dep) for dep in extra}
    for group in pyproject.get("dependency-groups", {}).values():
        names |= {_canonical(dep) for dep in group if isinstance(dep, str)}
    return names


def _referrers() -> dict[str, list[str]]:
    """Requirement name -> the pyprojects that require it."""
    found: dict[str, list[str]] = {}
    pyprojects = [REPO_ROOT / "pyproject.toml"]
    for member in ROOT_PYPROJECT["tool"]["uv"]["workspace"]["members"]:
        pyprojects.append(REPO_ROOT / member / "pyproject.toml")

    for path in pyprojects:
        assert path.is_file(), f"{path} is declared as a workspace member but does not exist"
        for name in _requirements(tomllib.loads(path.read_text())):
            found.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    return found


REFERRERS = _referrers()
SOURCE_NAMES = sorted(ROOT_PYPROJECT["tool"]["uv"]["sources"])


@pytest.mark.parametrize("source_name", SOURCE_NAMES)
def test_source_entry_has_a_dependent(source_name: str):
    referrers = REFERRERS.get(_canonical(source_name), [])

    assert referrers, (
        f"[tool.uv.sources] declares {source_name!r}, but no pyproject in the "
        f"workspace depends on it. A source entry without a dependent installs "
        f"nothing and never reaches uv.lock — delete it, or add the dependency "
        f"that makes it real."
    )
