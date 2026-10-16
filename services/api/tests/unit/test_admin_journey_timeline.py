"""Unit tests for the Journey stage timeline built from a story's recorded times."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from src.admin_console import build_timeline
from src.schemas.admin_console import TimelineStageState, TimelineTotalUnavailable

EPOCH = datetime.fromtimestamp(1_790_000_000, tz=UTC)


def at(minutes: float) -> datetime:
    return EPOCH + timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).isoformat().replace("+00:00", "Z")


def story(*, ci_runs=None, merged=None, created=-10):
    timeline = {"ci_runs": ci_runs or [], "pull_request": {"number": 7, "merged_at": merged}}
    return SimpleNamespace(created_at=at(created), generated_product_timeline=timeline)


def brief(confirmed=0, admitted=5):
    return SimpleNamespace(
        confirmed_at=None if confirmed is None else at(confirmed),
        coverage_admitted_at=None if admitted is None else at(admitted),
    )


def project(**config):
    return SimpleNamespace(config=config)


def run(type, start, end, *, status="completed", outcome=None):
    return SimpleNamespace(
        type=type,
        status=status,
        created_at=at(start if start is not None else 999),
        started_at=None if start is None else at(start),
        completed_at=None if end is None else at(end),
        result=None if outcome is None else {"qa_outcome": outcome},
    )


def ci(start, end):
    return {"id": 1, "started_at": iso(start), "completed_at": iso(end)}


def full_story_timeline():
    runs = [
        run("qa", 47, 50, outcome="passed"),
        run("engineering", 26, 30),
        run("deploy", 36, 40),
        run("qa", 41, 45, outcome="failed"),
        run("engineering", 10, 20, status="failed"),
    ]
    return build_timeline(
        story(ci_runs=[ci(21, 25), ci(31, 34)], merged=iso(35)),
        brief(confirmed=0, admitted=5),
        project(scaffold_started_at=iso(3), scaffold_finished_at=iso(9)),
        runs,
    )


def test_stages_of_every_kind_follow_their_starts_and_count_per_kind():
    timeline = full_story_timeline()

    assert [(s.kind, s.label) for s in timeline.stages] == [
        ("architect", "architect"),
        ("scaffold", "scaffold"),
        ("engineering", "engineering 1"),
        ("ci", "CI 1"),
        ("engineering", "engineering 2"),
        ("ci", "CI 2"),
        ("merge", "merge"),
        ("deploy", "deploy 1"),
        ("qa", "QA 1"),
        ("qa", "QA 2"),
    ]
    assert {s.state for s in timeline.stages} == {TimelineStageState.DONE}
    durations = {s.label: s.duration_seconds for s in timeline.stages}
    assert durations["architect"] == 300
    assert durations["CI 1"] == 240
    merge = timeline.stages[6]
    assert merge.started_at == merge.finished_at == at(35)
    assert merge.duration_seconds == 0


def test_waits_are_the_gaps_after_the_latest_end_and_an_overlap_makes_none():
    # The scaffold starts before the architect ends, so no wait sits between them.
    assert [(w.after, w.before, w.seconds) for w in full_story_timeline().waits] == [
        ("scaffold", "engineering 1", 60),
        ("engineering 1", "CI 1", 60),
        ("CI 1", "engineering 2", 60),
        ("engineering 2", "CI 2", 60),
        ("CI 2", "merge", 60),
        ("merge", "deploy 1", 60),
        ("deploy 1", "QA 1", 60),
        ("QA 1", "QA 2", 120),
    ]


def test_a_wait_counts_from_the_latest_end_not_from_a_shorter_stage_inside_it():
    timeline = build_timeline(
        story(ci_runs=[ci(12, 14)]),
        brief(confirmed=0, admitted=10),
        project(),
        [run("engineering", 10, 30), run("engineering", 33, 40)],
    )

    assert [(w.after, w.before, w.seconds) for w in timeline.waits] == [
        ("engineering 1", "engineering 2", 180)
    ]


def test_a_stage_with_a_start_and_no_end_is_running_and_covers_later_starts():
    timeline = build_timeline(
        story(ci_runs=[{"id": 1, "started_at": iso(20), "completed_at": None}]),
        brief(confirmed=0, admitted=5),
        project(),
        [run("engineering", 8, None, status="running")],
    )

    running = {s.label: s for s in timeline.stages if s.state is TimelineStageState.RUNNING}
    assert sorted(running) == ["CI 1", "engineering 1"]
    assert running["engineering 1"].started_at == at(8)
    assert running["engineering 1"].finished_at is None
    assert running["engineering 1"].duration_seconds is None
    assert [(w.after, w.before) for w in timeline.waits] == [("architect", "engineering 1")]


def test_a_past_story_shows_its_unrecorded_stages_after_the_timed_ones_in_kind_order():
    timeline = build_timeline(
        SimpleNamespace(
            created_at=at(-10),
            generated_product_timeline={"ci_runs": [{"id": 1}, {"id": 2}]},
        ),
        brief(confirmed=0, admitted=5),
        SimpleNamespace(config=None),
        [
            run("qa", None, None, status="queued"),
            run("deploy", 30, 35),
            run("engineering", 10, 20),
        ],
    )

    assert [(s.label, s.state) for s in timeline.stages] == [
        ("architect", "done"),
        ("engineering 1", "done"),
        ("deploy 1", "done"),
        ("scaffold", "unrecorded"),
        ("CI 1", "unrecorded"),
        ("CI 2", "unrecorded"),
        ("merge", "unrecorded"),
        ("QA 1", "unrecorded"),
    ]
    unrecorded = timeline.stages[3:]
    assert all(s.started_at is None and s.duration_seconds is None for s in unrecorded)
    assert timeline.total_unavailable is TimelineTotalUnavailable.NO_PASSED_QA


def test_a_scaffold_from_before_the_story_belongs_to_an_earlier_story():
    timeline = build_timeline(
        story(created=0),
        brief(),
        project(scaffold_started_at=iso(-60), scaffold_finished_at=iso(-50)),
        [],
    )

    assert "scaffold" not in [s.kind for s in timeline.stages]


def test_a_rerun_scaffold_runs_until_it_records_a_finish_after_its_new_start():
    timeline = build_timeline(
        story(),
        brief(),
        project(scaffold_started_at=iso(8), scaffold_finished_at=iso(4)),
        [],
    )

    [scaffold] = [s for s in timeline.stages if s.kind == "scaffold"]
    assert scaffold.state is TimelineStageState.RUNNING
    assert scaffold.finished_at is None


def test_the_total_runs_from_the_confirmed_brief_to_the_first_passed_qa():
    timeline = full_story_timeline()

    assert timeline.total_seconds == 50 * 60
    assert timeline.total_unavailable is None


@pytest.mark.parametrize(
    ("story_brief", "qa_outcomes", "reason"),
    [
        (None, ["passed"], TimelineTotalUnavailable.NO_BRIEF),
        (
            brief(confirmed=None, admitted=None),
            ["passed"],
            TimelineTotalUnavailable.BRIEF_NOT_CONFIRMED,
        ),
        (brief(), ["failed", "error"], TimelineTotalUnavailable.NO_PASSED_QA),
    ],
)
def test_the_total_names_why_it_is_unavailable(story_brief, qa_outcomes, reason):
    runs = [run("qa", 10 + i, 12 + i, outcome=o) for i, o in enumerate(qa_outcomes)]
    runs.append(run("qa", 20, None, status="running"))

    timeline = build_timeline(story(), story_brief, project(), runs)

    assert timeline.total_seconds is None
    assert timeline.total_unavailable is reason


@pytest.mark.parametrize(
    ("ci_runs", "merged", "config"),
    [
        ([{"id": 1, "started_at": "yesterday", "completed_at": None}], None, {}),
        ([{"id": 1, "started_at": iso(1), "completed_at": 1_790_000_600}], None, {}),
        ([], "soon", {}),
        ([], None, {"scaffold_started_at": "", "scaffold_finished_at": None}),
    ],
)
def test_a_recorded_time_that_is_not_iso_raises(ci_runs, merged, config):
    with pytest.raises(ValueError, match="ISO|isoformat"):
        build_timeline(story(ci_runs=ci_runs, merged=merged), brief(), project(**config), [])


def test_a_completed_qa_run_whose_result_is_not_a_qa_result_raises():
    damaged = run("qa", 10, 12, outcome="maybe")

    with pytest.raises(ValidationError):
        build_timeline(story(), brief(), project(), [damaged])
