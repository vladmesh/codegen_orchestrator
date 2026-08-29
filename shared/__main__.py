"""Canonical broad unit suite as one Python module entry point.

``python -m shared`` runs exactly what ``make test-unit`` runs: ``scripts/test-unit-local.sh`` from
the tree this ``shared`` package was imported from. The shell script stays the single owner of the
``ALL_SUITES`` table and of the fixture environment (``scripts/check-ci-gate.py`` reads the table
off it), so this module adds no second list to keep in sync.

Why a module and not the shell form: Secretary's ``check broad --module shared`` records which tree
the check imported ``shared`` from and can reuse the receipt on unchanged content; a
``--command 'make test-unit'`` receipt attests nothing and is re-run every time.

``shared`` is still not a package (``docs/decisions/shared-is-not-a-package.md``): this file only
gives the tree an executable entry point, it does not make it installable.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UNIT_SCRIPT = ROOT / "scripts" / "test-unit-local.sh"


def build_command(argv: list[str]) -> list[str]:
    """The exact argv ``python -m shared`` executes: the tree's unit runner plus caller flags."""
    return ["bash", str(UNIT_SCRIPT), *argv]


def build_env(base: dict[str, str], interpreter: str) -> dict[str, str]:
    """PATH with the running interpreter's directory first.

    The runner calls ``python -m pytest`` by name. Under ``uv run`` the venv is already first on
    PATH; under ``check broad`` only the interpreter is pinned, so the same venv is put first here
    and the suite cannot fall through to a system python without pytest.
    """
    env = dict(base)
    # Not resolved: the venv's ``python`` is a symlink into a toolchain dir that has no ``python``.
    interpreter_dir = str(Path(interpreter).absolute().parent)
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    if not entries or entries[0] != interpreter_dir:
        entries = [interpreter_dir, *[entry for entry in entries if entry != interpreter_dir]]
    env["PATH"] = os.pathsep.join(entries)
    env.setdefault("VIRTUAL_ENV", str(Path(interpreter_dir).parent))
    return env


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not UNIT_SCRIPT.is_file():
        print(f"unit runner not found: {UNIT_SCRIPT}", file=sys.stderr)
        return 2
    completed = subprocess.run(
        build_command(args), cwd=ROOT, env=build_env(dict(os.environ), sys.executable), check=False
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
