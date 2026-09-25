"""The one place every live test gets its `pytest-timeout` bound.

The numbers are `shared/stand_deadlines.py`'s, which also says why they are what
they are; this module only decides which of them an item gets. Each item gets a
*body* bound over its setup and call, set as its `timeout` marker, and a
*teardown* bound that `arm_teardown_bound` re-arms when the item's teardown
starts. A module-scoped lifecycle fixture runs its whole lifecycle in the setup
of the first item that uses it, so that item carries the lifecycle's waits.
"""

from dataclasses import dataclass
from pathlib import Path

from pipeline_helpers import SCRIPTED_AGENT_TYPE, level1_developer_agent_type
import pytest
from pytest_timeout import Settings

from shared.stand_deadlines import (
    BRIEF_TEST_BOUNDS,
    LIVE_TEST_BOUNDS,
    LIVE_TEST_TIMEOUT_METHOD,
    NOOP_TEST_BOUNDS,
    ORDINARY_TEST_BOUNDS,
    LiveTestBounds,
)

#: The module fixture each lifecycle suite's whole run lives in, by test module.
LEVEL1_LIFECYCLE = ("test_full_pipeline.py", "pipeline")
BRIEF_LIFECYCLES = (
    ("test_product_brief_pipeline.py", "product_brief_pipeline"),
    ("test_product_brief_package_pipeline.py", "product_brief_package_pipeline"),
)

TEARDOWN_TIMEOUT_KEY = pytest.StashKey[int]()
LIVE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ItemBound:
    body_seconds: int
    teardown_seconds: int


def level1_test_bounds() -> LiveTestBounds:
    """`mega-noop`'s bounds for the scripted developer, `mega-live`'s for a model."""
    if level1_developer_agent_type() == SCRIPTED_AGENT_TYPE:
        return NOOP_TEST_BOUNDS
    return LIVE_TEST_BOUNDS


def _lifecycle(item: pytest.Item) -> tuple[str, str] | None:
    module = item.path.name
    for lifecycle in (LEVEL1_LIFECYCLE, *BRIEF_LIFECYCLES):
        if lifecycle[0] == module and lifecycle[1] in getattr(item, "fixturenames", ()):
            return lifecycle
    return None


def item_bounds(items: list[pytest.Item]) -> list[ItemBound]:
    """Each item's bound, in order: the first user of a lifecycle fixture carries it."""
    bounds: list[ItemBound] = []
    set_up: set[tuple[str, str]] = set()
    for item in items:
        lifecycle = _lifecycle(item)
        if lifecycle is None:
            suite = ORDINARY_TEST_BOUNDS
        elif lifecycle == LEVEL1_LIFECYCLE:
            suite = level1_test_bounds()
        else:
            suite = BRIEF_TEST_BOUNDS
        sets_up = lifecycle is not None and lifecycle not in set_up
        if sets_up:
            set_up.add(lifecycle)
        bounds.append(
            ItemBound(
                body_seconds=suite.setup_item_seconds if sets_up else suite.item_seconds,
                teardown_seconds=suite.teardown_seconds,
            )
        )
    return bounds


def apply_bounds(items: list[pytest.Item], root: Path = LIVE_DIR) -> None:
    """Mark every item with its body bound and stash its teardown bound.

    Only items under `root`, `tests/live` itself: the collection hook is
    session-wide, so a run that collects other directories beside this one hands
    it their items too.
    A live test carrying its own `timeout` marker is refused: the bounds are
    derived in one ledger, and a second source would be one nobody checks.
    """
    live = [item for item in items if item.path.resolve().is_relative_to(root)]
    for item, bound in zip(live, item_bounds(live), strict=True):
        if item.get_closest_marker("timeout") is not None:
            raise pytest.UsageError(
                f"{item.nodeid} sets its own timeout; live test bounds come from "
                "shared/stand_deadlines.py through tests/live/live_timeouts.py"
            )
        item.add_marker(pytest.mark.timeout(bound.body_seconds, method=LIVE_TEST_TIMEOUT_METHOD))
        item.stash[TEARDOWN_TIMEOUT_KEY] = bound.teardown_seconds


def arm_teardown_bound(item: pytest.Item) -> None:
    """Replace the item's timer with its own teardown bound.

    pytest-timeout's timer would otherwise span the teardown with whatever the
    body left — or, after a failed setup or call, not at all: it cancels the
    timer on `pytest_exception_interact`. Its `pytest_runtest_protocol` still
    cancels this one when the item ends.
    """
    hooks = item.config.pluginmanager.hook
    hooks.pytest_timeout_cancel_timer(item=item)
    hooks.pytest_timeout_set_timer(
        item=item,
        settings=Settings(
            timeout=float(item.stash[TEARDOWN_TIMEOUT_KEY]),
            method=LIVE_TEST_TIMEOUT_METHOD,
            func_only=False,
            disable_debugger_detection=False,
        ),
    )
