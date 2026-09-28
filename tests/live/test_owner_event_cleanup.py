"""A run's cleanup takes back the PO's latest-owner-event records of its own stories.

Run 36383692107 completed both stories and its residue proof named
`po:latest_owner_event:970539079:story-32bffa85` and the second story's twin: the
PO consumer keeps that record without a TTL, and the proof is right to name a key
that carries the run's story. These drive the real `cleanup_and_prove` over one
fake Redis that both the removals and the proof read, with the keys built by the
PO's own function.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

import db_teardown
from live_harness import CleanupError, OwnershipManifest, cleanup_guard
import pipeline_helpers
import po_checkpoints
import pytest
import redis_cli_fake
import run_residue

from services.langgraph.src.agents.po import tools_notices

pytestmark = pytest.mark.needs_no_api_credential

RUN_ID = "live-c3b5f0b95e1e"
PROJECT_ID = "project-1421"
STORY_ID = "story-32bffa85"
EXTENSION_STORY_ID = "story-6d7d8267"
NEIGHBOUR_STORY_ID = "story-0a1b2c3d"
#: A story id that begins with one of the run's: its keys are not the run's either.
PREFIX_SHARING_STORY_ID = f"{STORY_ID}b"
CHAT_ID = "970539079"
OTHER_CHAT_ID = "999000001"


def _event_keys(story_id: str) -> list[str]:
    return [
        tools_notices.latest_notice_key(CHAT_ID, story_id),
        tools_notices.latest_notice_key(OTHER_CHAT_ID, story_id),
    ]


@pytest.fixture
def cli(monkeypatch) -> redis_cli_fake.FakeRedisCli:
    """One Redis behind every removal and every read; nothing else of the stack."""
    cli = redis_cli_fake.FakeRedisCli()
    monkeypatch.setattr(pipeline_helpers, "_redis_command", cli)
    monkeypatch.setattr(
        run_residue,
        "host_residue_ops",
        lambda *_args: (redis_cli_fake.residue_ops(cli), lambda _e: None),
    )
    monkeypatch.setattr(po_checkpoints, "remove_run_rows", lambda *_args: [])

    async def cleanup_all(_api_internal, _api_observer, _ctx):
        return db_teardown.TeardownReport(selection=PROJECT_ID)

    monkeypatch.setattr(pipeline_helpers, "cleanup_all", cleanup_all)
    return cli


def _write_event_keys(cli: redis_cli_fake.FakeRedisCli, story_id: str) -> list[str]:
    keys = _event_keys(story_id)
    for key in keys:
        cli.redis.set(key, "{}")
    return keys


def _run_ctx() -> dict:
    return {
        "manifest": OwnershipManifest(RUN_ID),
        "project_id": PROJECT_ID,
        "story_id": STORY_ID,
        "level1_extension": {"story_id": EXTENSION_STORY_ID},
        "po_thread_id": f"po-chat-{CHAT_ID}",
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
async def test_cleanup_removes_the_owner_event_records_and_the_proof_passes(cli, abort):
    _write_event_keys(cli, STORY_ID)
    _write_event_keys(cli, EXTENSION_STORY_ID)
    neighbour = _write_event_keys(cli, NEIGHBOUR_STORY_ID)
    ctx = _run_ctx()

    if abort:
        with pytest.raises(RuntimeError, match="aborted mid-story"):
            await _run(ctx, abort=True)
    else:
        await _run(ctx, abort=False)

    checks = {check["kind"]: check for check in ctx["run_residue"]["checks"]}
    assert {check["outcome"] for check in checks.values()} == {"absent"}, checks
    assert checks["redis_keys"]["findings"] == []
    # Nothing of the run is left; another story's records are the PO's.
    assert sorted(cli.redis.keys("*")) == sorted(neighbour)


async def test_without_the_release_the_proof_names_the_records_the_run_left(cli, monkeypatch):
    """The gap run 36383692107 hit, kept visible: the proof is what catches it."""
    monkeypatch.setattr(pipeline_helpers, "release_story_owner_events", lambda _ctx: None)
    left = _write_event_keys(cli, EXTENSION_STORY_ID)
    ctx = _run_ctx()

    with pytest.raises(CleanupError) as raised:
        await _run(ctx, abort=False)

    for key in left:
        assert key in str(raised.value)


def test_the_patterns_select_exactly_the_keys_the_po_writes_for_a_story():
    patterns = pipeline_helpers.owner_event_key_patterns(STORY_ID)

    for key in _event_keys(STORY_ID):
        assert any(fnmatchcase(key, pattern) for pattern in patterns), key
    for other in (NEIGHBOUR_STORY_ID, EXTENSION_STORY_ID, PREFIX_SHARING_STORY_ID):
        for key in _event_keys(other):
            assert not any(fnmatchcase(key, pattern) for pattern in patterns), key


def test_a_record_that_survives_removal_fails_cleanup(cli, monkeypatch):
    """A removal that did not take is said here, not left for the proof to guess at."""
    _write_event_keys(cli, STORY_ID)

    def ignoring_deletes(*args: str) -> str:
        return "0" if args[0] == "UNLINK" else cli(*args)

    monkeypatch.setattr(pipeline_helpers, "_redis_command", ignoring_deletes)

    with pytest.raises(CleanupError, match="PO owner-event records of stories"):
        pipeline_helpers.release_story_owner_events({**_run_ctx(), "level1_extension": None})
