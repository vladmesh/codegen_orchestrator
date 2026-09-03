"""Every `python3 -m scripts.*` line in the stand workflow runs on a bare host.

Those invocations use the machine's system interpreter — the runner's, and on
the target the one behind `ssh ... python3` — not the repository environment.
That interpreter has the standard library and nothing else, so a host-side
script may only import modules that are stdlib-only transitively.

Nothing enforced that until run 33739202480 died at `ModuleNotFoundError: No
module named 'pydantic'` before the suite had started: the previous round had
given `scripts/register_bitlaunch_target.py` a `shared.contracts` import for one
constant. Unit tests stayed green because no test ever ran those scripts under
the interpreter the workflow uses.

The list of scripts is read out of the workflow file itself. A hand-written list
would buy nothing: the next invocation someone adds has to be covered without
anyone remembering this file exists.

What this proves and what it does not: it proves each named module *imports*
with no third-party package available. It does not run the scripts, and it says
nothing about their behaviour on the stand host.
"""

from pathlib import Path
import re
import subprocess
import sys

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "stand-e2e.yml"

HOST_INVOCATION = re.compile(r"python3\s+-m\s+scripts\.([A-Za-z_][A-Za-z0-9_]*)")

# The ones the workflow ran when this guard was written. They are asserted to be
# a subset of what the parse finds, so a parse that silently stops matching is a
# failure instead of a vacuous pass. They are never the enumeration itself.
KNOWN_HOST_MODULES = frozenset(
    {
        "register_bitlaunch_target",
        "request_stand_provisioning",
        "stand_acceptance",
        "stand_credentials",
        "stand_dns",
        "stand_lifecycle",
        "wait_stand_provisioning",
    }
)


def _host_modules() -> frozenset[str]:
    """Enumerate `python3 -m scripts.<module>` from the workflow's own shell."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    found: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            script = step.get("run")
            if script:
                found.update(HOST_INVOCATION.findall(script))
    return frozenset(found)


def _import_under_bare_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    """Import one module with site-packages off, exactly what the host has.

    `-S` drops the site machinery, and with it every installed distribution,
    while leaving the standard library where the host's interpreter has it.
    `PYTHONPATH` supplies the checkout, which is how the workflow itself reaches
    `scripts` and `shared` — neither is an installed package there either.
    """
    return subprocess.run(
        [sys.executable, "-S", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT,
    )


def test_the_workflow_parse_finds_the_known_host_invocations() -> None:
    modules = _host_modules()

    assert KNOWN_HOST_MODULES <= modules, sorted(KNOWN_HOST_MODULES - modules)


def test_the_bare_interpreter_really_has_no_third_party_package() -> None:
    """Without this the guard below could pass by never denying anything."""
    result = _import_under_bare_interpreter("pydantic")

    assert result.returncode != 0
    assert "No module named 'pydantic'" in result.stderr


def test_every_host_side_script_imports_without_third_party_packages() -> None:
    offenders = {}
    for module in sorted(_host_modules()):
        result = _import_under_bare_interpreter(f"scripts.{module}")
        if result.returncode != 0:
            offenders[module] = result.stderr.strip().splitlines()[-1]

    assert not offenders, (
        "these stand-e2e scripts cannot import under the host's system interpreter: "
        + "; ".join(f"{module}: {error}" for module, error in offenders.items())
    )
