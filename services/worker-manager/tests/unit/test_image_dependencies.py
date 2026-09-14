"""The worker-manager image installs only its own declared dependencies.

Unit tests run in the repository environment, which holds every service's
packages, so a `shared` module that needs a package this service never declared
imports fine here and crashes the container at startup. This test follows the
imports reachable from `src.main` through `src` and `shared` and requires every
third-party package to be in this service's locked dependency closure.
"""

import ast
from pathlib import Path
import sys
import tomllib

SERVICE = Path(__file__).resolve().parents[2]
ROOT = SERVICE.parents[1]

# Import names whose distribution is spelled differently.
_DISTRIBUTION = {"yaml": "pyyaml", "jwt": "pyjwt", "nacl": "pynacl"}


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def _installed(*, without: frozenset[str] = frozenset()) -> set[str]:
    """This service's locked dependency closure: what `pip install .` provides.

    A package imported directly but only installed transitively (pydantic via
    fastapi) is present in the image; a package absent from the closure is not.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {_normalize(package["name"]): package for package in lock["package"]}
    pending = [
        dependency
        for dependency in packages["worker-manager"]["dependencies"]
        if _normalize(dependency["name"]) not in without
    ]
    installed: set[str] = set()
    while pending:
        dependency = pending.pop()
        name = _normalize(dependency["name"])
        package = packages[name]
        extras = [
            extra
            for extra in dependency.get("extra", [])
            if extra in package.get("optional-dependencies", {})
        ]
        if name in installed and not extras:
            continue
        installed.add(name)
        pending.extend(package.get("dependencies", []))
        for extra in extras:
            pending.extend(package["optional-dependencies"][extra])
    return installed


def _module_path(module: str) -> Path | None:
    parts = module.split(".")
    base = SERVICE if parts[0] == "src" else ROOT
    for candidate in (
        base.joinpath(*parts).with_suffix(".py"),
        base.joinpath(*parts, "__init__.py"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, package: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = package.split(".")
                base = ".".join(parent[: len(parent) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
                names.add(target)
                names.update(f"{target}.{alias.name}" for alias in node.names)
            elif node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _reachable_third_party() -> dict[str, str]:
    """Third-party top-level import -> first first-party module that imports it."""
    seen: set[str] = set()
    pending = ["src.main"]
    third_party: dict[str, str] = {}
    while pending:
        module = pending.pop()
        path = _module_path(module)
        if module in seen or path is None:
            continue
        seen.add(module)
        # Importing a package module runs its parents' __init__ files first.
        parents = module.split(".")
        pending.extend(".".join(parents[:depth]) for depth in range(1, len(parents)))
        package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
        for name in _imports(path, package):
            root = name.split(".")[0]
            if root in {"src", "shared"}:
                pending.append(name)
            elif root not in sys.stdlib_module_names and root != "__future__":
                third_party.setdefault(root, module)
    return third_party


def _missing(installed: set[str]) -> dict[str, str]:
    return {
        root: importer
        for root, importer in _reachable_third_party().items()
        if _normalize(_DISTRIBUTION.get(root, root)) not in installed
    }


def test_every_package_reachable_from_main_is_installed_in_the_image():
    missing = _missing(_installed())

    assert missing == {}, f"packages imported from src.main but absent from the image: {missing}"


def test_the_guard_catches_the_undeclared_notification_transport():
    """Regression: shared.notifications made the image crash on a missing aiohttp."""
    assert _missing(_installed(without=frozenset({"aiohttp"}))) == {
        "aiohttp": "shared.notifications"
    }
