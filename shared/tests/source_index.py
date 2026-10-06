"""One read and one parse of the repository's Python sources per pytest process.

The architectural guards (an import or call boundary over the whole tree) each used
to walk and parse the same files on their own. They read the tree through this
index instead: file lists, texts and ASTs are built on first use and kept for the
rest of the session, so a suite pays for each file once. Callers must not mutate
the trees they get.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@cache
def python_files(root: Path) -> tuple[Path, ...]:
    """Every `*.py` file under root, sorted, without bytecode caches."""
    return tuple(sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts))


@cache
def service_sources() -> tuple[Path, ...]:
    """Production code of every service: `services/*/src/**/*.py`."""
    return tuple(
        path for src in sorted((ROOT / "services").glob("*/src")) for path in python_files(src)
    )


@cache
def shared_sources() -> tuple[Path, ...]:
    """The shared tree without its tests."""
    return tuple(p for p in python_files(ROOT / "shared") if "tests" not in p.parts)


@cache
def text(path: Path) -> str:
    return path.read_text()


@cache
def tree(path: Path) -> ast.Module:
    return ast.parse(text(path), filename=str(path))


@cache
def nodes(path: Path) -> tuple[ast.AST, ...]:
    """Every node of the file's tree, in `ast.walk` order."""
    return tuple(ast.walk(tree(path)))


@pytest.fixture(scope="session")
def production_source_index() -> None:
    """Read, parse and walk every service and shared module once, as session setup.

    A guard that scans the tree requests it, so the parse is the suite's cost, paid
    once, rather than the first guard's. A conftest imports this fixture to offer it.
    """
    for path in (*service_sources(), *shared_sources()):
        nodes(path)
