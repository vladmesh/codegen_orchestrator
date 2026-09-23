"""A story in work tells its owner which stage it is at — once per entry, once per interval.

The sweep runs against a real `RedisStreamClient` over `fakeredis`, so the
notice asserted on is the one read back off `po:input` and the marker is the one
a restarted scheduler would read. The clock is passed in, never slept on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_project, _make_story
from fakeredis import FakeServer, aioredis
from pydantic import ValidationError
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.story import (
    STAGE_NOTICE_OWNER_TOLD_STATUSES,
    STAGE_NOTICE_STATUSES,
    STAGE_NOTICE_TERMINAL_STATUSES,
    WAITING_ON_BY_STATUS,
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
    story_wait_estimate,
)
from shared.contracts.dto.user import UserDTO
from shared.contracts.queues.po import POSystemEvent
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src.tasks.supervisor.stage_notices import (
    MARKED_STORIES_KEY,
    read_stage_notice_marker,
    stage_notice_key,
    supervise_stage_notices,
)

STORY_ID = "story-stage-1"
QUIET_MINUTES = 60
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class _Stories:
    """The API's view of story statuses, moved by the test between sweeps."""

    def __init__(self) -> None:
        self.by_id: dict[str, StoryStatus] = {}

    def put(self, status: StoryStatus, story_id: str = STORY_ID) -> None:
        self.by_id[story_id] = status

    async def by_status(self, status):
        return [
            _make_story(id=story_id, status=StoryStatus(status).value)
            for story_id, current in self.by_id.items()
            if current is StoryStatus(status)
        ]

    async def get(self, story_id: str):
        """The story as it is now: what the sweep reads again before it announces."""
        return _make_story(id=story_id, status=self.by_id[story_id].value)


@pytest.fixture
def stories() -> _Stories:
    return _Stories()


@pytest.fixture
def api_client(stories):
    client = AsyncMock()
    client.get_stories_by_status.side_effect = stories.by_status
    client.get_story.side_effect = stories.get
    client.get_project.return_value = _make_project()
    client.get_user.return_value = UserDTO(id=1, telegram_id=4242, created_at=T0)
    return client


@pytest.fixture
def redis_server() -> FakeServer:
    return FakeServer()


def _redis_client(server: FakeServer) -> RedisStreamClient:
    """A scheduler's Redis client; two built over one server are one Redis."""
    client = RedisStreamClient(redis_url="redis://localhost:6379/0")
    client._redis = aioredis.FakeRedis(server=server, decode_responses=True)
    return client


@pytest.fixture
def redis_client(redis_server) -> RedisStreamClient:
    return _redis_client(redis_server)


async def _notices(redis_client: RedisStreamClient) -> list[POSystemEvent]:
    """Every stage notice on `po:input`, parsed with the contract PO parses with."""
    entries = await redis_client.redis.xrange(PO_INPUT_QUEUE)
    return [
        POSystemEvent.model_validate(fields)
        for _, fields in entries
        if fields.get("event") == OwnerNotificationEvent.STORY_STAGE.value
    ]


# ── the notice on entering a stage ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "estimate"),
    [
        # Bounded in `STATE_AGE_BOUNDS`: 30, 60 and 220 minutes in the conftest.
        (StoryStatus.DEPLOYING, StoryWaitEstimate.TENS_OF_MINUTES),
        (StoryStatus.TESTING, StoryWaitEstimate.TENS_OF_MINUTES),
        (StoryStatus.PR_REVIEW, StoryWaitEstimate.HOURS),
        # No configured bound: said so, not invented.
        (StoryStatus.CREATED, StoryWaitEstimate.UNBOUNDED),
        (StoryStatus.IN_PROGRESS, StoryWaitEstimate.UNBOUNDED),
        (StoryStatus.REOPENED, StoryWaitEstimate.UNBOUNDED),
    ],
)
async def test_entering_a_stage_sends_one_notice_naming_it(
    api_client, redis_client, stories, stage, estimate
):
    stories.put(stage)

    counts = await supervise_stage_notices(api_client, redis_client, now=T0)

    assert counts == {"entered": 1, "still_there": 0, "unaddressable": 0}
    [notice] = await _notices(redis_client)
    assert notice.story_id == STORY_ID
    assert notice.telegram_chat_id == "4242"
    assert notice.stage is stage
    assert notice.waiting_on is WAITING_ON_BY_STATUS[stage]
    assert notice.wait_estimate is estimate
    assert notice.stage_notice is StoryStageNoticeKind.ENTERED


@pytest.mark.asyncio
async def test_the_estimate_follows_the_states_configured_bound(api_client, redis_client, stories):
    """Raising the deploy bound raises the estimate: one number, not two."""
    from src import startup

    stories.put(StoryStatus.DEPLOYING)
    original = startup.config.get_int.side_effect

    def bound(key):
        return 300 if key == "supervisor.deploy_wait_max_minutes" else original(key)

    startup.config.get_int.side_effect = bound

    await supervise_stage_notices(api_client, redis_client, now=T0)

    [notice] = await _notices(redis_client)
    assert notice.wait_estimate is StoryWaitEstimate.HOURS
    assert "300 minutes" in notice.text


@pytest.mark.asyncio
async def test_one_notice_per_entry_not_one_per_tick(api_client, redis_client, stories):
    stories.put(StoryStatus.IN_PROGRESS)

    for tick in range(6):
        await supervise_stage_notices(api_client, redis_client, now=_at(tick * 0.5))

    assert len(await _notices(redis_client)) == 1


# ── the repeat after the quiet interval ──────────────────────────────────


@pytest.mark.asyncio
async def test_still_in_the_stage_after_the_quiet_interval_sends_another(
    api_client, redis_client, stories
):
    stories.put(StoryStatus.IN_PROGRESS)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    counts = await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES))

    assert counts == {"entered": 0, "still_there": 1, "unaddressable": 0}
    entered, still_there = await _notices(redis_client)
    assert entered.stage_notice is StoryStageNoticeKind.ENTERED
    assert still_there.stage_notice is StoryStageNoticeKind.STILL_THERE
    assert still_there.stage is StoryStatus.IN_PROGRESS
    assert "60 minutes since the last update" in still_there.text


@pytest.mark.asyncio
async def test_nothing_is_sent_twice_inside_one_interval(api_client, redis_client, stories):
    """The interval runs from the last notice, so the repeat also opens a new one."""
    stories.put(StoryStatus.PR_REVIEW)
    await supervise_stage_notices(api_client, redis_client, now=T0)
    await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES - 0.5))
    await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES))
    await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES + 1))
    await supervise_stage_notices(api_client, redis_client, now=_at(2 * QUIET_MINUTES - 0.5))

    kinds = [notice.stage_notice for notice in await _notices(redis_client)]
    assert kinds == [StoryStageNoticeKind.ENTERED, StoryStageNoticeKind.STILL_THERE]


@pytest.mark.asyncio
async def test_a_stage_change_restarts_the_interval(api_client, redis_client, stories):
    stories.put(StoryStatus.DEPLOYING)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    stories.put(StoryStatus.TESTING)
    await supervise_stage_notices(api_client, redis_client, now=_at(10))
    # One interval after the *deploying* notice, but not after the testing one.
    await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES))
    await supervise_stage_notices(api_client, redis_client, now=_at(10 + QUIET_MINUTES))

    notices = [(n.stage, n.stage_notice) for n in await _notices(redis_client)]
    assert notices == [
        (StoryStatus.DEPLOYING, StoryStageNoticeKind.ENTERED),
        (StoryStatus.TESTING, StoryStageNoticeKind.ENTERED),
        (StoryStatus.TESTING, StoryStageNoticeKind.STILL_THERE),
    ]


# ── a stage the story has left since the scan ────────────────────────────


def _moves_after_the_scan(api_client, stories, to: StoryStatus, story_id: str = STORY_ID):
    """Routing moves the story on after the sweep's scan and before its publish."""
    scanned: set[StoryStatus] = set()

    async def scanned_then_moved(status):
        observed = await stories.by_status(status)
        scanned.add(StoryStatus(status))
        if scanned == STAGE_NOTICE_STATUSES:
            stories.put(to, story_id)
        return observed

    api_client.get_stories_by_status.side_effect = scanned_then_moved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "moved_to"),
    [
        (StoryStatus.DEPLOYING, StoryStatus.TESTING),
        (StoryStatus.TESTING, StoryStatus.COMPLETED),
        (StoryStatus.PR_REVIEW, StoryStatus.DEPLOYING),
    ],
)
async def test_a_stage_left_after_the_scan_is_not_announced(
    api_client, redis_client, stories, observed, moved_to
):
    stories.put(observed)
    _moves_after_the_scan(api_client, stories, moved_to)

    counts = await supervise_stage_notices(api_client, redis_client, now=T0)

    assert counts == {"entered": 0, "still_there": 0, "unaddressable": 0}
    assert await _notices(redis_client) == []
    assert await read_stage_notice_marker(redis_client, STORY_ID) is None
    assert await redis_client.redis.smembers(MARKED_STORIES_KEY) == set()


@pytest.mark.asyncio
async def test_the_next_sweep_announces_the_stage_the_story_moved_to(
    api_client, redis_client, stories
):
    stories.put(StoryStatus.DEPLOYING)
    _moves_after_the_scan(api_client, stories, StoryStatus.TESTING)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    api_client.get_stories_by_status.side_effect = stories.by_status
    counts = await supervise_stage_notices(api_client, redis_client, now=_at(0.5))

    assert counts == {"entered": 1, "still_there": 0, "unaddressable": 0}
    [notice] = await _notices(redis_client)
    assert notice.stage is StoryStatus.TESTING
    assert notice.stage_notice is StoryStageNoticeKind.ENTERED
    assert (await read_stage_notice_marker(redis_client, STORY_ID)).stage is StoryStatus.TESTING


@pytest.mark.asyncio
async def test_a_repeat_for_a_stage_left_after_the_scan_keeps_the_old_marker(
    api_client, redis_client, stories
):
    """No `still_there` for the old stage, and its marker is not renewed as if it were sent."""
    stories.put(StoryStatus.IN_PROGRESS)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    _moves_after_the_scan(api_client, stories, StoryStatus.PR_REVIEW)
    counts = await supervise_stage_notices(api_client, redis_client, now=_at(QUIET_MINUTES))

    assert counts == {"entered": 0, "still_there": 0, "unaddressable": 0}
    assert [n.stage_notice for n in await _notices(redis_client)] == [StoryStageNoticeKind.ENTERED]
    marker = await read_stage_notice_marker(redis_client, STORY_ID)
    assert marker.stage is StoryStatus.IN_PROGRESS
    assert marker.notified_at == T0


@pytest.mark.asyncio
async def test_one_storys_move_does_not_silence_another(api_client, redis_client, stories):
    stories.put(StoryStatus.DEPLOYING, "story-moved")
    stories.put(StoryStatus.IN_PROGRESS, "story-staying")
    _moves_after_the_scan(api_client, stories, StoryStatus.TESTING, "story-moved")

    counts = await supervise_stage_notices(api_client, redis_client, now=T0)

    assert counts == {"entered": 1, "still_there": 0, "unaddressable": 0}
    assert [(n.story_id, n.stage) for n in await _notices(redis_client)] == [
        ("story-staying", StoryStatus.IN_PROGRESS)
    ]
    assert await redis_client.redis.smembers(MARKED_STORIES_KEY) == {"story-staying"}


@pytest.mark.asyncio
async def test_a_failed_re_read_is_that_storys_failed_notice(api_client, redis_client, stories):
    stories.put(StoryStatus.DEPLOYING, "story-unreadable")
    stories.put(StoryStatus.IN_PROGRESS, "story-readable")

    async def unreadable(story_id):
        if story_id == "story-unreadable":
            raise RuntimeError("api unavailable")
        return await stories.get(story_id)

    api_client.get_story.side_effect = unreadable
    counts = await supervise_stage_notices(api_client, redis_client, now=T0)

    assert counts == {"entered": 1, "still_there": 0, "unaddressable": 0}
    assert [n.story_id for n in await _notices(redis_client)] == ["story-readable"]
    assert await read_stage_notice_marker(redis_client, "story-unreadable") is None


# ── the two ways the notices stop ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", sorted(STAGE_NOTICE_TERMINAL_STATUSES))
async def test_notices_stop_at_a_terminal_state(api_client, redis_client, stories, ending):
    stories.put(StoryStatus.TESTING)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    stories.put(ending)
    for minutes in (1, QUIET_MINUTES, 3 * QUIET_MINUTES):
        counts = await supervise_stage_notices(api_client, redis_client, now=_at(minutes))
        assert counts == {"entered": 0, "still_there": 0, "unaddressable": 0}

    assert [n.stage for n in await _notices(redis_client)] == [StoryStatus.TESTING]
    asked = {call.args[0] for call in api_client.get_stories_by_status.await_args_list}
    assert not asked & STAGE_NOTICE_TERMINAL_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize("parked", sorted(STAGE_NOTICE_OWNER_TOLD_STATUSES))
async def test_notices_stop_while_the_owner_has_been_told_what_is_needed(
    api_client, redis_client, stories, parked
):
    stories.put(StoryStatus.DEPLOYING)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    stories.put(parked)
    for minutes in (1, QUIET_MINUTES, 3 * QUIET_MINUTES):
        await supervise_stage_notices(api_client, redis_client, now=_at(minutes))

    assert [n.stage for n in await _notices(redis_client)] == [StoryStatus.DEPLOYING]
    assert await read_stage_notice_marker(redis_client, STORY_ID) is None


@pytest.mark.asyncio
async def test_coming_back_from_an_owner_wait_is_a_new_entry(api_client, redis_client, stories):
    """Back in the same stage minutes later is still announced: the owner saw it stop."""
    stories.put(StoryStatus.DEPLOYING)
    await supervise_stage_notices(api_client, redis_client, now=T0)
    stories.put(StoryStatus.WAITING_USER_SECRET)
    await supervise_stage_notices(api_client, redis_client, now=_at(2))
    stories.put(StoryStatus.DEPLOYING)
    await supervise_stage_notices(api_client, redis_client, now=_at(5))

    kinds = [(n.stage, n.stage_notice) for n in await _notices(redis_client)]
    assert kinds == [
        (StoryStatus.DEPLOYING, StoryStageNoticeKind.ENTERED),
        (StoryStatus.DEPLOYING, StoryStageNoticeKind.ENTERED),
    ]


# ── durability across a scheduler restart ────────────────────────────────


@pytest.mark.asyncio
async def test_a_restart_neither_repeats_nor_resets_the_interval(api_client, redis_server, stories):
    stories.put(StoryStatus.IN_PROGRESS)
    before = _redis_client(redis_server)
    await supervise_stage_notices(api_client, before, now=T0)
    await before.redis.aclose()

    # A new scheduler process: new client, same Redis, nothing held in memory.
    after = _redis_client(redis_server)
    first_tick = await supervise_stage_notices(api_client, after, now=_at(1))
    mid_interval = await supervise_stage_notices(api_client, after, now=_at(QUIET_MINUTES - 1))
    due = await supervise_stage_notices(api_client, after, now=_at(QUIET_MINUTES))

    assert first_tick == {"entered": 0, "still_there": 0, "unaddressable": 0}
    assert mid_interval == {"entered": 0, "still_there": 0, "unaddressable": 0}
    assert due == {"entered": 0, "still_there": 1, "unaddressable": 0}
    marker = await read_stage_notice_marker(after, STORY_ID)
    assert marker.stage is StoryStatus.IN_PROGRESS
    assert marker.notified_at == _at(QUIET_MINUTES)
    # No expiry: nothing but the story leaving work ends the marker.
    assert await after.redis.ttl(stage_notice_key(STORY_ID)) == -1


@pytest.mark.asyncio
async def test_an_outage_longer_than_any_old_expiry_still_owes_the_repeat(
    api_client, redis_server, stories
):
    """Scheduler down three quiet intervals with Redis up: the clock is the last notice.

    The first cut expired the marker after two intervals, so this restart sent
    `entered` as if the story had just arrived. The due notice is `still_there`.
    """
    stories.put(StoryStatus.IN_PROGRESS)
    before = _redis_client(redis_server)
    await supervise_stage_notices(api_client, before, now=T0)
    await before.redis.aclose()
    # Stand-in for the wall clock passing: an expiring marker would be gone now.
    await redis_server_time_passes(redis_server, minutes=3 * QUIET_MINUTES)

    after = _redis_client(redis_server)
    counts = await supervise_stage_notices(api_client, after, now=_at(3 * QUIET_MINUTES))
    again = await supervise_stage_notices(api_client, after, now=_at(3 * QUIET_MINUTES + 1))

    assert counts == {"entered": 0, "still_there": 1, "unaddressable": 0}
    assert again == {"entered": 0, "still_there": 0, "unaddressable": 0}
    kinds = [notice.stage_notice for notice in await _notices(after)]
    assert kinds == [StoryStageNoticeKind.ENTERED, StoryStageNoticeKind.STILL_THERE]


async def redis_server_time_passes(server: FakeServer, *, minutes: float) -> None:
    """Expire every key whose TTL would have run out in *minutes* of wall time.

    fakeredis expires on its own real clock; the sweep's clock is passed in. So
    the outage is applied to Redis explicitly: any key carrying a TTL no longer
    than the outage is removed, exactly as a real Redis would have removed it.
    """
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    for key in await client.keys("*"):
        ttl = await client.ttl(key)
        if 0 <= ttl <= minutes * 60:
            await client.delete(key)
    await client.aclose()


@pytest.mark.asyncio
async def test_markers_are_forgotten_exactly_when_their_story_leaves_work(
    api_client, redis_client, stories
):
    stories.put(StoryStatus.TESTING, "story-ending")
    stories.put(StoryStatus.DEPLOYING, "story-parked")
    stories.put(StoryStatus.IN_PROGRESS, "story-working")
    await supervise_stage_notices(api_client, redis_client, now=T0)
    assert await redis_client.redis.smembers(MARKED_STORIES_KEY) == {
        "story-ending",
        "story-parked",
        "story-working",
    }

    stories.put(StoryStatus.COMPLETED, "story-ending")
    stories.put(StoryStatus.WAITING_HUMAN_REVIEW, "story-parked")
    await supervise_stage_notices(api_client, redis_client, now=_at(1))

    assert await redis_client.redis.smembers(MARKED_STORIES_KEY) == {"story-working"}
    assert await read_stage_notice_marker(redis_client, "story-ending") is None
    assert await read_stage_notice_marker(redis_client, "story-parked") is None
    assert (await read_stage_notice_marker(redis_client, "story-working")).stage is (
        StoryStatus.IN_PROGRESS
    )


@pytest.mark.asyncio
async def test_a_failed_read_forgets_nothing(api_client, redis_client, stories):
    """A sweep that could not read every in-work stage must not take absence for an ending."""
    stories.put(StoryStatus.DEPLOYING)
    await supervise_stage_notices(api_client, redis_client, now=T0)

    async def testing_unreadable(status):
        if StoryStatus(status) is StoryStatus.TESTING:
            raise RuntimeError("api unavailable")
        return await stories.by_status(status)

    api_client.get_stories_by_status.side_effect = testing_unreadable
    with pytest.raises(RuntimeError):
        await supervise_stage_notices(api_client, redis_client, now=_at(1))

    assert (await read_stage_notice_marker(redis_client, STORY_ID)).stage is StoryStatus.DEPLOYING
    assert await redis_client.redis.smembers(MARKED_STORIES_KEY) == {STORY_ID}


# ── what the notice is not ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_owner_without_a_chat_is_not_published_to_and_not_retried(
    api_client, redis_client, stories
):
    stories.put(StoryStatus.IN_PROGRESS)
    api_client.get_user.return_value = None

    with patch("src.tasks._recipients.notify_admins_best_effort", new_callable=AsyncMock) as admin:
        first = await supervise_stage_notices(api_client, redis_client, now=T0)
        second = await supervise_stage_notices(api_client, redis_client, now=_at(1))

    assert first == {"entered": 0, "still_there": 0, "unaddressable": 1}
    assert second == {"entered": 0, "still_there": 0, "unaddressable": 0}
    assert await _notices(redis_client) == []
    admin.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_stage_notice_is_never_an_owed_record(api_client, redis_client, stories):
    """The terminal seam is not used, and would refuse the event if it were."""
    stories.put(StoryStatus.TESTING)
    with (
        patch(
            "src.tasks.owner_notifications.owe_story_owner_notification", new_callable=AsyncMock
        ) as owe_story,
        patch(
            "src.tasks.owner_notifications.owe_owner_notification", new_callable=AsyncMock
        ) as owe_run,
    ):
        await supervise_stage_notices(api_client, redis_client, now=T0)
    owe_story.assert_not_awaited()
    owe_run.assert_not_awaited()
    api_client.update_story.assert_not_awaited()
    api_client.update_run.assert_not_awaited()

    with pytest.raises(ValidationError, match="never an owed owner notification"):
        OwnerNotification(
            event=OwnerNotificationEvent.STORY_STAGE,
            text="stage",
            story_id=STORY_ID,
            project_id="p",
            terminal_status=StoryStatus.TESTING,
            state=OwnerNotificationState.OWED,
            owed_at=T0,
        )


def test_the_three_stage_sets_partition_every_story_status():
    sets = (STAGE_NOTICE_STATUSES, STAGE_NOTICE_TERMINAL_STATUSES, STAGE_NOTICE_OWNER_TOLD_STATUSES)
    assert set().union(*sets) == set(StoryStatus)
    assert sum(len(s) for s in sets) == len(StoryStatus)


def test_the_estimate_is_the_magnitude_of_the_bound():
    assert story_wait_estimate(None) is StoryWaitEstimate.UNBOUNDED
    assert story_wait_estimate(5) is StoryWaitEstimate.MINUTES
    assert story_wait_estimate(30) is StoryWaitEstimate.TENS_OF_MINUTES
    assert story_wait_estimate(60) is StoryWaitEstimate.TENS_OF_MINUTES
    assert story_wait_estimate(220) is StoryWaitEstimate.HOURS
    assert story_wait_estimate(1440) is StoryWaitEstimate.DAYS


def test_a_stage_notice_carries_the_typed_stage_it_names():
    fields = {
        "event": OwnerNotificationEvent.STORY_STAGE,
        "text": "stage",
        "story_id": STORY_ID,
        "stage": StoryStatus.DEPLOYING,
        "waiting_on": WAITING_ON_BY_STATUS[StoryStatus.DEPLOYING],
        "wait_estimate": StoryWaitEstimate.TENS_OF_MINUTES,
        "stage_notice": StoryStageNoticeKind.ENTERED,
    }
    POSystemEvent(**fields)
    with pytest.raises(ValidationError, match="waits on deploy"):
        POSystemEvent(**{**fields, "waiting_on": WAITING_ON_BY_STATUS[StoryStatus.TESTING]})
    with pytest.raises(ValidationError, match="not a stage a story is in work in"):
        POSystemEvent(
            **{
                **fields,
                "stage": StoryStatus.COMPLETED,
                "waiting_on": WAITING_ON_BY_STATUS[StoryStatus.COMPLETED],
            }
        )
    with pytest.raises(ValidationError, match="carries stage"):
        POSystemEvent(**{**fields, "wait_estimate": None})
    with pytest.raises(ValidationError, match="story_stage only"):
        POSystemEvent(**{**fields, "event": OwnerNotificationEvent.STORY_COMPLETED})
