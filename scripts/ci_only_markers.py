"""pytest plugin: the ci_only marker family, one umbrella and six reasons.

`scripts/test-unit-local.sh` loads it (`-p scripts.ci_only_markers`) for every suite.
A test that a weak control host must not run carries `ci_only`, or one of the
sub-markers that name why: `docker`, `ansible`, `privileged`, `kit_gate`,
`subprocess` and `slow(reason=...)`. Each
sub-marker implies the umbrella, so the host profile's `-m "not ci_only"` deselects
the whole family. CI runs the runner without that expression, so every marked test
still runs there. The markers are registered in the root `pyproject.toml` and in
every service `pytest.ini` too, so `--strict-markers` holds without this plugin.

`--refuse-ci-only-selection` (the host profile passes it when it runs a caller's
selectors, `python -m shared -- <selector>...`): a selector whose every test the
`-m` expression deselected for the family is a usage error naming the marker,
not an empty run that passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CI_ONLY = "ci_only"
CI_ONLY_SUB_MARKERS = {
    "docker": "runs the real docker CLI (compose config, build, info)",
    "ansible": "runs ansible-core for real (ansible-playbook, PlaybookCLI)",
    "privileged": "needs root or sudo and changes the machine (useradd, become)",
    "kit_gate": "runs the product kit's toolchain gate (generate, ruff, xenon, deptry)",
    "subprocess": "starts many processes (bash scripts, git, make, python children)",
    "slow": "needs more than the 0.5 s unit budget; slow(reason=...) says why",
}
CI_ONLY_FAMILY = frozenset({CI_ONLY, *CI_ONLY_SUB_MARKERS})

# Selectors are shown from the checkout root: the runner passes them absolute.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DESELECTED = pytest.StashKey[list[pytest.Item]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("ci_only").addoption(
        "--refuse-ci-only-selection",
        action="store_true",
        default=False,
        help="fail when a selector reaches only ci_only-family tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{CI_ONLY}: runs in CI only, never in the host profile")
    for name, reason in CI_ONLY_SUB_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: implies {CI_ONLY}; {reason}")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Add the umbrella before `-m` deselects, so a sub-marker alone is enough."""
    for item in items:
        slow = item.get_closest_marker("slow")
        if slow is not None and not slow.kwargs.get("reason"):
            raise pytest.UsageError(f"{item.nodeid}: @pytest.mark.slow needs reason=...")
        if item.get_closest_marker(CI_ONLY) is None and any(
            item.get_closest_marker(name) for name in CI_ONLY_SUB_MARKERS
        ):
            item.add_marker(CI_ONLY)


def pytest_deselected(items: list[pytest.Item]) -> None:
    if items:
        config = items[0].config
        config.stash.setdefault(_DESELECTED, []).extend(items)


def _family_markers(item: pytest.Item) -> list[str]:
    subs = [name for name in sorted(CI_ONLY_SUB_MARKERS) if item.get_closest_marker(name)]
    return subs or ([CI_ONLY] if item.get_closest_marker(CI_ONLY) else [])


def _selects(selector: str, base: Path, item: pytest.Item) -> bool:
    path, _, node = selector.partition("::")
    target = (base / path).resolve()
    if item.path.resolve() != target and target not in item.path.resolve().parents:
        return False
    if not node:
        return True
    _, _, item_node = item.nodeid.partition("::")
    return item_node == node or item_node.startswith((f"{node}::", f"{node}["))


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    if not config.getoption("refuse_ci_only_selection"):
        return
    base = config.invocation_params.dir
    deselected = [item for item in config.stash.get(_DESELECTED, []) if _family_markers(item)]
    for selector in config.args:
        if any(_selects(selector, base, item) for item in session.items):
            continue
        markers = sorted(
            {
                name
                for item in deselected
                if _selects(selector, base, item)
                for name in _family_markers(item)
            }
        )
        if markers:
            shown = selector.removeprefix(f"{_REPO_ROOT}/")
            raise pytest.UsageError(
                f"{shown} selects only tests marked {', '.join(markers)} ({CI_ONLY} family): "
                "they run in CI, not in the host profile"
            )
