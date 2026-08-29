"""A script that imports `shared` must be started as a module, never by path.

`python scripts/x.py` puts `scripts/` on `sys.path`, not the repository root, so
the script cannot import `shared` — which is not an installed package and can
only be found from the tree. The failure is a bare `ModuleNotFoundError: No
module named 'shared'` at the first call site, which reads as a missing
dependency rather than a wrong invocation.

This class has now stopped three runs: `stand_lifecycle` importing
`scripts.bitlaunch_stand` on 2026-08-28, then `stand_preflight` importing
`shared` on the same day, then `stand_preflight` again on 2026-08-29 after the
fix was lost. Each time it was repaired at the one call site that had failed.

So this checks the class, not a site: every script that imports `shared` is
invoked as `-m`, everywhere it is invoked.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).parents[2]
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
IMPORTS_SHARED = re.compile(r"^\s*(?:from|import)\s+shared\b", re.MULTILINE)
# The stand path only. Other scripts share the defect and are a separate
# cleanup; guarding them here would fail on code this change does not touch.
STAND_SCRIPTS = {
    "stand_preflight",
    "stand_run",
    "stand_lifecycle",
    "stand_dns",
    "clean_live_tests",
    "register_bitlaunch_target",
    "wait_stand_provisioning",
    "request_stand_provisioning",
}


def _scripts_importing_shared() -> set[str]:
    return {
        path.stem
        for path in SCRIPTS.glob("*.py")
        if path.stem in STAND_SCRIPTS and IMPORTS_SHARED.search(path.read_text())
    }


def _callers() -> list[tuple[str, str]]:
    sources = [(str(p.relative_to(REPO_ROOT)), p.read_text()) for p in SCRIPTS.glob("*.py")]
    sources += [(str(p.relative_to(REPO_ROOT)), p.read_text()) for p in WORKFLOWS.glob("*.yml")]
    sources.append(("Makefile", (REPO_ROOT / "Makefile").read_text()))
    return sources


def test_every_shared_importing_script_is_invoked_as_a_module() -> None:
    offenders = []
    for name in sorted(_scripts_importing_shared()):
        by_path = re.compile(rf"scripts/{re.escape(name)}\.py")
        for where, text in _callers():
            for line in text.splitlines():
                if not by_path.search(line):
                    continue
                # A path is fine as data — a docstring, a comment, a file the
                # code reads. It is the invocation that must not use one.
                if re.search(r"(python3?|sys\.executable|\"\]|uv run)", line) and "-m" not in line:
                    offenders.append((where, name, line.strip()))
    assert not offenders, (
        "these call a script that imports `shared` by path, so it will fail with "
        f"ModuleNotFoundError instead of running: {offenders}"
    )


def test_the_guard_knows_about_the_scripts_it_guards() -> None:
    """A silent empty set would make the test above pass by accident."""
    found = _scripts_importing_shared()
    assert "stand_preflight" in found, (
        "stand_preflight no longer imports shared, or the scan stopped finding it"
    )
