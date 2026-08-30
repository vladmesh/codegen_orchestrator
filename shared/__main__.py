"""Canonical broad unit suite entry point for the in-tree shared package."""

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
    """Put the requested interpreter's environment first on PATH."""
    env = dict(base)
    # Preserve the venv directory when its Python executable is a symlink.
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
