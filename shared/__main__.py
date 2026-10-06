"""Canonical broad unit suite entry point for the in-tree shared package."""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
UNIT_SCRIPT = ROOT / "scripts" / "test-unit-local.sh"
# The broad check runs on the weak control host, so it is the runner's light host
# profile; CI runs the full fan-out through `make test-unit` instead.
HOST_PROFILE_FLAG = "--host"
# CPU seconds (user + sys, every process the runner waits for) the host profile may use
# on the control host: the measured run plus about 30 % (docs/TESTING.md). A change that
# breaks it moves tests to CI (a ci_only-family marker) rather than raising the budget.
HOST_CPU_BUDGET_SECONDS = 240.0


def build_command(argv: list[str]) -> list[str]:
    """The exact argv ``python -m shared`` executes: the host profile plus caller flags."""
    return ["bash", str(UNIT_SCRIPT), HOST_PROFILE_FLAG, *argv]


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


def suite_cpu(cpu_dir: Path) -> dict[str, float]:
    """CPU seconds per suite label, as each suite's pytest wrote them (unit_test_budget)."""
    return {
        path.stem: float(json.loads(path.read_text())["cpu_seconds"])
        for path in sorted(cpu_dir.glob("*.json"))
    }


def children_cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def cpu_report(per_suite: dict[str, float], total: float, budget: float) -> list[str]:
    lines = ["CPU per suite (user+sys, children included):"]
    lines += [
        f"  {label:<28} {seconds:7.1f}s"
        for label, seconds in sorted(per_suite.items(), key=lambda kv: -kv[1])
    ]
    lines.append(f"CPU total: {total:.1f}s (budget {budget:g}s)")
    if total > budget:
        lines.append(
            f"FAILED: the unit run used {total:.1f}s CPU, over its {budget:g}s budget. "
            "Move the heavy tests to CI with a ci_only-family marker (docs/TESTING.md)."
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not UNIT_SCRIPT.is_file():
        print(f"unit runner not found: {UNIT_SCRIPT}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="unit-cpu-") as cpu_dir:
        env = build_env(dict(os.environ), sys.executable)
        env["UNIT_CPU_DIR"] = cpu_dir
        before = children_cpu()
        completed = subprocess.run(build_command(args), cwd=ROOT, env=env, check=False)
        total = children_cpu() - before
        per_suite = suite_cpu(Path(cpu_dir))
    print("\n".join(cpu_report(per_suite, total, HOST_CPU_BUDGET_SECONDS)), flush=True)
    if completed.returncode == 0 and total > HOST_CPU_BUDGET_SECONDS:
        return 1
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
