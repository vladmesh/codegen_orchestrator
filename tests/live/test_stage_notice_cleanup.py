"""A run's cleanup takes back the scheduler's stage-notice markers of its own stories.

Run 36218333606 aborted mid-story and its residue proof named
`story:stage_notice:story-cc7f05bd`: the sweep deletes a marker only once it sees
the story settle, and an aborted run's story never does. These drive the real
`cleanup_and_prove` — the fence release, the marker release and the residue proof
— over one fake Redis that both the removals and the proof read.
"""

from __future__ import annotations

import db_teardown
import fakeredis
from live_harness import CleanupError, OwnershipManifest, cleanup_guard
import pipeline_helpers
import po_checkpoints
import pytest
import run_residue

from services.scheduler.src.tasks.supervisor import stage_notices
from shared.live_harness_cleanup import RESIDUE_FINDINGS_KEY
from shared.queues import STORY_WORKERS_KEY

pytestmark = pytest.mark.needs_no_api_credential

RUN_ID = "live-5e7a6e0c1381"
PROJECT_ID = "project-1381"
STORY_ID = "story-cc7f05bd"
EXTENSION_STORY_ID = "story-0e1f2a3b"
NEIGHBOUR_STORY_ID = "story-neighbour"


class FakeRedisCli:
    """`redis-cli` as `_redis_command` calls it, answering with its stdout."""

    def __init__(self) -> None:
        self.redis = fakeredis.FakeRedis(decode_responses=True)

    def __call__(self, *args: str) -> str:
        result = self.redis.execute_command(*args)
        if result is None:
            return ""
        if isinstance(result, list):
            return "\n".join(str(item) for item in result)
        return str(result)

    def mark(self, story_id: str) -> None:
        """What the sweep writes for a story in work: marker and membership together."""
        self.redis.set(stage_notices.stage_notice_key(story_id), '{"stage": "in_progress"}')
        self.redis.sadd(stage_notices.MARKED_STORIES_KEY, story_id)


def _residue_ops(cli: FakeRedisCli) -> run_residue.ResidueOps:
    return run_residue.ResidueOps(
        run_labelled_containers=lambda _run: [],
        compose_project_containers=lambda _project: [],
        off_host_residue=lambda _inventory: {
            kind: {RESIDUE_FINDINGS_KEY: []}
            for kind in ("github_repository", "registry_repositories", "target_containers")
        },
        workspace_entries=lambda _entries: [],
        redis_keys=lambda patterns: sorted(
            {key for pattern in patterns for key in cli.redis.scan_iter(match=pattern)}
        ),
        story_worker_bindings=lambda stories: [
            story for story in stories if cli.redis.hget(STORY_WORKERS_KEY, story)
        ],
        po_checkpoint_rows=lambda _inventory: [],
    )


@pytest.fixture
def cli(monkeypatch) -> FakeRedisCli:
    """One Redis behind every removal and every read; nothing else of the stack."""
    cli = FakeRedisCli()
    monkeypatch.setattr(pipeline_helpers, "_redis_command", cli)
    monkeypatch.setattr(
        run_residue, "host_residue_ops", lambda *_args: (_residue_ops(cli), lambda _e: None)
    )
    monkeypatch.setattr(po_checkpoints, "remove_run_rows", lambda *_args: [])

    async def cleanup_all(_api_internal, _api_observer, _ctx):
        return db_teardown.TeardownReport(selection=PROJECT_ID)

    monkeypatch.setattr(pipeline_helpers, "cleanup_all", cleanup_all)
    return cli


def _run_ctx() -> dict:
    return {
        "manifest": OwnershipManifest(RUN_ID),
        "project_id": PROJECT_ID,
        "story_id": STORY_ID,
        "level1_extension": {"story_id": EXTENSION_STORY_ID},
        "po_thread_id": "po-chat-999000001",
        "po_checkpoint_snapshot": {"checkpoints": []},
    }


async def _run(ctx: dict, *, abort: bool) -> None:
    async with cleanup_guard(
        lambda: pipeline_helpers.cleanup_and_prove(object(), None, ctx),
        manifest=ctx["manifest"],
    ):
        if abort:
            raise RuntimeError("the run aborted mid-story")


@pytest.mark.parametrize("abort", [True, False], ids=["aborted", "completed"])
async def test_cleanup_removes_the_runs_stage_notice_markers_and_proves_nothing_left(cli, abort):
    cli.mark(STORY_ID)
    cli.mark(EXTENSION_STORY_ID)
    cli.mark(NEIGHBOUR_STORY_ID)
    cli.redis.set(f"live:work:cancelled:{PROJECT_ID}", "1")
    ctx = _run_ctx()

    if abort:
        # Only the run's own error: cleanup and its proof succeeded, so it is
        # not wrapped in a group with a cleanup failure.
        with pytest.raises(RuntimeError, match="aborted mid-story"):
            await _run(ctx, abort=True)
    else:
        await _run(ctx, abort=False)

    checks = {check["kind"]: check for check in ctx["run_residue"]["checks"]}
    assert set(checks) == set(run_residue.RESIDUE_KINDS)
    assert {check["outcome"] for check in checks.values()} == {"absent"}, checks
    assert checks["redis_keys"]["findings"] == []
    for story_id in (STORY_ID, EXTENSION_STORY_ID):
        assert not cli.redis.exists(stage_notices.stage_notice_key(story_id))
        assert not cli.redis.sismember(stage_notices.MARKED_STORIES_KEY, story_id)
    # Another run's story is still in work, and its marker is the sweep's.
    assert cli.redis.exists(stage_notices.stage_notice_key(NEIGHBOUR_STORY_ID))
    assert cli.redis.smembers(stage_notices.MARKED_STORIES_KEY) == {NEIGHBOUR_STORY_ID}


async def test_without_the_release_the_proof_names_the_marker_the_aborted_run_left(
    cli, monkeypatch
):
    """The gap run 36218333606 hit, kept visible: the proof is what catches it."""
    monkeypatch.setattr(pipeline_helpers, "release_story_stage_notices", lambda _ctx: None)
    cli.mark(STORY_ID)
    ctx = _run_ctx()

    with pytest.raises(BaseExceptionGroup) as raised:
        await _run(ctx, abort=True)

    cleanup_error = raised.value.exceptions[1]
    assert isinstance(cleanup_error, CleanupError)
    assert f"story:stage_notice:{STORY_ID}" in str(cleanup_error)


def test_a_run_that_owns_no_story_asks_redis_nothing(cli, monkeypatch):
    ctx = _run_ctx()
    del ctx["story_id"], ctx["level1_extension"]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(pipeline_helpers, "_redis_command", lambda *args: calls.append(args) or "")

    pipeline_helpers.release_story_stage_notices(ctx)

    assert calls == []


def test_a_marker_that_survives_removal_fails_cleanup(cli, monkeypatch):
    """A removal that did not take is said here, not left for the proof to guess at."""
    cli.mark(STORY_ID)

    def ignoring_deletes(*args: str) -> str:
        return "0" if args[0] in {"UNLINK", "SREM"} else cli(*args)

    monkeypatch.setattr(pipeline_helpers, "_redis_command", ignoring_deletes)

    with pytest.raises(CleanupError, match=f"stage-notice markers of stories \\['{STORY_ID}'\\]"):
        pipeline_helpers.release_story_stage_notices({**_run_ctx(), "level1_extension": None})


def test_the_harness_names_the_keys_the_sweep_writes():
    assert pipeline_helpers.STAGE_NOTICE_KEY_PREFIX == stage_notices.STAGE_NOTICE_KEY_PREFIX
    assert pipeline_helpers.STAGE_NOTICE_MARKED_STORIES_KEY == stage_notices.MARKED_STORIES_KEY
    assert stage_notices.stage_notice_key(STORY_ID) == (
        f"{pipeline_helpers.STAGE_NOTICE_KEY_PREFIX}{STORY_ID}"
    )
