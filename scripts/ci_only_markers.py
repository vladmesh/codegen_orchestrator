"""pytest plugin: the ci_only marker family, one umbrella and four reasons.

`scripts/test-unit-local.sh` loads it (`-p scripts.ci_only_markers`) for every suite.
A test that a weak control host must not run carries `ci_only`, or one of the
sub-markers that name why: `docker`, `ansible`, `privileged`, `kit_gate`. Each
sub-marker implies the umbrella, so the host profile's `-m "not ci_only"` deselects
the whole family. CI runs the runner without that expression, so every marked test
still runs there. The markers are registered in the root `pyproject.toml` and in
every service `pytest.ini` too, so `--strict-markers` holds without this plugin.
"""

from __future__ import annotations

import pytest

CI_ONLY = "ci_only"
CI_ONLY_SUB_MARKERS = {
    "docker": "runs the real docker CLI (compose config, build, info)",
    "ansible": "runs ansible-core for real (ansible-playbook, PlaybookCLI)",
    "privileged": "needs root or sudo and changes the machine (useradd, become)",
    "kit_gate": "runs the product kit's toolchain gate (generate, ruff, xenon, deptry)",
}
CI_ONLY_FAMILY = frozenset({CI_ONLY, *CI_ONLY_SUB_MARKERS})


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{CI_ONLY}: runs in CI only, never in the host profile")
    for name, reason in CI_ONLY_SUB_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: implies {CI_ONLY}; {reason}")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Add the umbrella before `-m` deselects, so a sub-marker alone is enough."""
    for item in items:
        if item.get_closest_marker(CI_ONLY) is None and any(
            item.get_closest_marker(name) for name in CI_ONLY_SUB_MARKERS
        ):
            item.add_marker(CI_ONLY)
