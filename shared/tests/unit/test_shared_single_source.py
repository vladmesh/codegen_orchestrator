"""`shared` must resolve to the repo tree and nowhere else.

`shared` used to be declared as an editable workspace dependency, but its
hatchling config had `packages = []` plus a `force-include` block, so the
"editable" wheel degraded into a file copy under site-packages. Which version a
test imported then depended on cwd: a suite run from a service directory got the
frozen copy, a suite run from the repo root got the tree, and the copy was only
refreshed by `uv sync --reinstall-package shared`.

`shared` is no longer a package. These tests pin that: nothing installs it, and
it is importable only when the repo root is on the path.
"""

from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

REPO_ROOT = Path(__file__).parents[3]
SHARED_INIT = REPO_ROOT / "shared" / "__init__.py"


def _run_import_probe(pythonpath: list[Path]) -> subprocess.CompletedProcess[str]:
    """Import `shared` in a child process with an explicit path.

    `-P` keeps the script directory and cwd off `sys.path`, so what remains is
    exactly PYTHONPATH plus the interpreter's own site directories.
    """
    return subprocess.run(
        [sys.executable, "-P", "-c", "import shared; print(shared.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": ":".join(str(p) for p in pythonpath),
        },
    )


def test_shared_is_not_installed_in_site_packages():
    for key in ("purelib", "platlib"):
        site_packages = Path(sysconfig.get_paths()[key])
        assert not (site_packages / "shared").exists(), (
            f"an installed copy of shared exists at {site_packages / 'shared'}; "
            "shared is delivered by the tree only"
        )
        assert not list(site_packages.glob("shared-*.dist-info")), (
            f"shared is registered as an installed distribution in {site_packages}"
        )


def test_shared_is_unimportable_without_the_repo_root():
    """No shadow copy: drop the repo root and the import must fail outright."""
    completed = _run_import_probe([REPO_ROOT / "services" / "api"])

    assert completed.returncode != 0, (
        f"shared imported from {completed.stdout.strip()} without the repo root "
        "on the path — a second copy of shared exists"
    )
    assert "No module named 'shared'" in completed.stderr, completed.stderr


@pytest.mark.parametrize(
    "service_dir",
    [
        "services/api",
        "services/langgraph",
        "services/telegram_bot",
        "services/scheduler",
        "services/worker-manager",
        "services/infra-service",
        "services/scaffolder",
        "packages/worker-wrapper",
    ],
)
def test_shared_resolves_to_the_tree_from_every_service_path(service_dir: str):
    """The per-service path never wins over the tree, whatever the entry order."""
    completed = _run_import_probe([REPO_ROOT / service_dir, REPO_ROOT])

    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == SHARED_INIT
