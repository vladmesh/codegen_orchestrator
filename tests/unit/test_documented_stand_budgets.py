"""Every stand budget a document states is the constant it is made of.

Card 1316 made the `mega-noop` ledger *be* the waits the harness spends, and
`scripts/tests/test_stand_run.py` checks the runner against that ledger entry by
entry. What nothing checked is the other direction: the minutes written down in
`tests/live/README.md`, in `docs/TESTING.md` and in the runner's own comments.
They drifted exactly as a number nobody checks does — the suite table still
billed `mega-brief` at the 281-minute cap it had before 2026-09-04, when the
variant moved to a 50-minute productive deadline with a separate cleanup grace,
and `scripts/stand_run.py` still explained its job cap with that same 297-minute
runner path.

So each assertion below reads a stated number out of a document and compares it
with the constant it describes. A budget that moves takes the document with it
or fails here; a document that is reworded past these anchors fails here too,
which is the cheaper of the two mistakes.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

from scripts import stand_run
from scripts.stand_run import SUITES
from shared import stand_deadlines

ROOT = Path(__file__).parents[2]
LIVE_README = ROOT / "tests" / "live" / "README.md"
TESTING_DOC = ROOT / "docs" / "TESTING.md"
STAND_RUNNER = ROOT / "scripts" / "stand_run.py"
LEVEL1_SUITE = ROOT / "tests" / "live" / "test_full_pipeline.py"
LEVEL1_CLASS = "TestFullPipeline"


def _one(pattern: str, path: Path) -> re.Match[str]:
    """The single place a document states one budget, or a loud failure."""
    matches = list(re.finditer(pattern, path.read_text(encoding="utf-8")))
    assert len(matches) == 1, (
        f"{path.name} states {pattern!r} {len(matches)} times; this check reads exactly one. "
        "If the sentence moved, move the anchor with it."
    )
    return matches[0]


def _minutes(value: str) -> int:
    return int(value) * 60


def _mmss(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


def _addends(listed: str) -> list[int]:
    return [int(one) for one in listed.replace("`", "").split("+")]


def _cap_cell(name: str) -> str:
    """How the suite table has to spell one suite's pytest cap."""
    suite = SUITES[name]
    stated = f"{suite.timeout_seconds // 60} min"
    if suite.cleanup_grace_seconds:
        stated += f" + {suite.cleanup_grace_seconds // 60} min grace"
    return stated + (" per cell" if suite.combinations else "")


def _table_rows() -> dict[str, list[str]]:
    rows = {}
    for line in LIVE_README.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        name = cells[1].strip("`")
        if name in SUITES:
            rows[name] = cells
    return rows


def test_the_suite_table_states_each_suites_own_pytest_cap() -> None:
    rows = _table_rows()

    assert set(rows) == set(SUITES), "the suite table no longer lists every named suite"
    for name, cells in rows.items():
        assert cells[7] == _cap_cell(name), (
            f"the README bills {name} at {cells[7]!r}; the runner spends {_cap_cell(name)!r}"
        )


def test_the_readme_states_the_noop_ledger_it_is_derived_from() -> None:
    ledger = _one(
        r"The ledger sums to (\d+)m(\d+)s and the\n(\d+)-minute cap leaves (\d+)m(\d+)s",
        LIVE_README,
    )
    reserve = _one(r"at least the (\d+)m(\d+)s\nreserve", LIVE_README)

    explicit = stand_deadlines.noop_lifecycle_explicit_waits()
    assert _mmss(ledger[1], ledger[2]) == explicit
    assert _minutes(ledger[3]) == stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS
    assert _mmss(ledger[4], ledger[5]) == stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS - explicit
    assert _mmss(reserve[1], reserve[2]) == stand_deadlines.NOOP_TEARDOWN_RESERVE_SECONDS


def test_the_readme_spells_each_part_of_the_lifecycle_as_the_waits_it_is_made_of() -> None:
    first = _one(r"\*\*first story\*\* spends (\d+)m(\d+)s \(`([\d +\n]+?)`", LIVE_README)
    second = _one(r"story on the same project\*\* spends (\d+)m(\d+)s \(`([\d +]+)`\)", LIVE_README)
    teardown = _one(r"\*\*Teardown\*\*\nspends (\d+)m:", LIVE_README)

    for stated, waits in (
        (first, stand_deadlines.NOOP_FIRST_STORY_WAITS),
        (second, stand_deadlines.NOOP_SECOND_STORY_WAITS),
    ):
        booked = [seconds for _label, seconds in waits]
        assert _addends(stated[3]) == booked, (
            "the README lists waits the ledger does not spend, in this order: "
            f"{_addends(stated[3])} against {booked}"
        )
        assert _mmss(stated[1], stated[2]) == sum(booked)
    assert _minutes(teardown[1]) == sum(
        seconds for _label, seconds in stand_deadlines.NOOP_TEARDOWN_WAITS
    )


def test_the_readme_states_the_job_path_the_workflow_actually_gives_a_run() -> None:
    noop_job = _one(r"comes to (\d+) of the workflow's (\d+) job-minutes", LIVE_README)
    matrix = _one(r"The matrix runner is bounded at (\d+) minutes", LIVE_README)
    cap = _one(r"The E2E job cap is (\d+) minutes, a strict\n(\d+)-minute reserve", LIVE_README)
    cleanup_job = _one(r"Lifecycle cleanup runs in its own (\d+)-minute GitHub job", LIVE_README)
    provisioning = _one(r"provisioning has a (\d+)-minute budget", LIVE_README)

    fixed = (
        stand_run.STAND_PROVISIONING_TIMEOUT_SECONDS
        + stand_run.STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
    )
    noop_path = (
        fixed
        + stand_run.PREFLIGHT_TIMEOUT_SECONDS
        + stand_run.READINESS_TIMEOUT_SECONDS
        + stand_run.EXECUTOR_SWITCH_TIMEOUT_SECONDS
        + stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS
        + stand_run.SWEEP_TIMEOUT_SECONDS
        + stand_run.STAND_JOB_RESERVE_SECONDS
    )
    assert _minutes(noop_job[1]) == noop_path
    assert _minutes(noop_job[2]) == stand_run.STAND_JOB_TIMEOUT_MINUTES * 60
    assert _minutes(matrix[1]) == stand_run.MATRIX_RUNNER_TIMEOUT_SECONDS
    assert _minutes(cap[1]) == stand_run.STAND_JOB_TIMEOUT_MINUTES * 60
    assert _minutes(cap[2]) == (
        stand_run.STAND_JOB_TIMEOUT_MINUTES * 60 - fixed - stand_run.MATRIX_RUNNER_TIMEOUT_SECONDS
    )
    assert int(cleanup_job[1]) == stand_run.STAND_CLEANUP_JOB_TIMEOUT_MINUTES
    assert _minutes(provisioning[1]) == stand_run.STAND_PROVISIONING_TIMEOUT_SECONDS


def test_both_documents_state_the_brief_variants_own_windows() -> None:
    readme_digest = _one(
        r"`mega-brief` stops its productive work at (\d+) minutes[^.]*?\n"
        r"then gets a (\d+)-minute cleanup grace",
        LIVE_README,
    )
    readme_package = _one(
        r"productive window — (\d+) minutes, then a (\d+)-minute cleanup grace", LIVE_README
    )
    testing_digest = _one(
        r"productive work stops at (\d+) minutes, then its fixture gets a separate\n"
        r"(\d+)-minute evidence-and-cleanup grace",
        TESTING_DOC,
    )
    testing_package = _one(
        r"it gets (\d+) productive minutes and a (\d+)-minute grace", TESTING_DOC
    )

    digest_grace = stand_deadlines.MEGA_BRIEF_HARD_STOP_SECONDS - (
        stand_deadlines.MEGA_BRIEF_PRODUCTIVE_SECONDS
    )
    package_grace = stand_deadlines.MEGA_BRIEF_PACKAGE_HARD_STOP_SECONDS - (
        stand_deadlines.MEGA_BRIEF_PACKAGE_PRODUCTIVE_SECONDS
    )
    for stated in (readme_digest, testing_digest):
        assert _minutes(stated[1]) == stand_deadlines.MEGA_BRIEF_PRODUCTIVE_SECONDS
        assert _minutes(stated[2]) == digest_grace
    for stated in (readme_package, testing_package):
        assert _minutes(stated[1]) == stand_deadlines.MEGA_BRIEF_PACKAGE_PRODUCTIVE_SECONDS
        assert _minutes(stated[2]) == package_grace


def test_the_runner_comment_explains_the_job_cap_with_the_runner_paths_it_has() -> None:
    covered = _one(
        r"# (\d+) minutes covers (\d+)m provisioning \+ (\d+)m workflow reserve", STAND_RUNNER
    )
    longest = _one(r"The longest path is the matrix \((\d+)m\)", STAND_RUNNER)
    briefs = _one(
        r"Product Brief runners are (\d+)m and (\d+)m, and `mega-noop` is (\d+)m", STAND_RUNNER
    )
    reserve = _one(r"an (\d+)m job reserve", STAND_RUNNER)

    def runner_path(suite_seconds: int) -> int:
        return (
            stand_run.PREFLIGHT_TIMEOUT_SECONDS
            + stand_run.READINESS_TIMEOUT_SECONDS
            + stand_run.EXECUTOR_SWITCH_TIMEOUT_SECONDS
            + suite_seconds
            + stand_run.SWEEP_TIMEOUT_SECONDS
        )

    assert int(covered[1]) == stand_run.STAND_JOB_TIMEOUT_MINUTES
    assert _minutes(covered[2]) == stand_run.STAND_PROVISIONING_TIMEOUT_SECONDS
    assert _minutes(covered[3]) == stand_run.STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
    assert _minutes(longest[1]) == stand_run.MATRIX_RUNNER_TIMEOUT_SECONDS
    assert _minutes(briefs[1]) == stand_run.BRIEF_RUNNER_TIMEOUT_SECONDS
    assert _minutes(briefs[2]) == stand_run.BRIEF_PACKAGE_RUNNER_TIMEOUT_SECONDS
    assert _minutes(briefs[3]) == runner_path(stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS)
    assert _minutes(reserve[1]) == stand_run.STAND_JOB_RESERVE_SECONDS


def test_the_testing_doc_counts_the_level1_tests_the_class_actually_has() -> None:
    """The tier table's numbers are the suite's, checked against the suite.

    It billed `mega-noop` at "~3" tests and "~7-10 min" — the shape it had
    before the level-1 product, its confirmed brief and its second story.
    """
    tree = ast.parse(LEVEL1_SUITE.read_text(encoding="utf-8"))
    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == LEVEL1_CLASS
    ]
    assert len(classes) == 1
    tests = [
        node
        for node in classes[0].body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("test_")
    ]

    row = _one(
        r"\| Full \(level 1\) \| `test-live-mega-noop` \| (\d+) \|[^|]*?(\d+) min cap \|",
        TESTING_DOC,
    )

    assert int(row[1]) == len(tests)
    assert _minutes(row[2]) == stand_deadlines.NOOP_SUITE_TIMEOUT_SECONDS
