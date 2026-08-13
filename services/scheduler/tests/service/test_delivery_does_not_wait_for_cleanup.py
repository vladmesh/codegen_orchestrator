"""A finished product is handed over while its temporary access is still being taken back.

Everything here is built through the real API and read back from it. The story,
its QA run and the grant are durable records; the tick under test starts from
them, exactly as the scheduler does.

The delay is not simulated with sleeps. It does not have to be: the record
refuses to call the access gone until readings taken over
``REVOKE_CONFIRMATION_WINDOW`` (ten minutes) agree, so a grant whose revoke
deploy has only just succeeded stays live for at least that long by
construction. What these tests assert is that none of it reaches the user's
story — it completes on the tick that reads the QA verdict, the owner is told
then, and nothing the cleanup does afterwards touches it again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.config_store import ConfigStore
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationResult,
    env_observation_result_key,
)
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, ENV_OBSERVATION_QUEUE

HEAD_SHA = "d" * 40
ENV_KEY = "TG_BOT_TEST_TELEGRAM_ID"
DEPLOYED_URL = "https://delivered.example.com"
BOT_USERNAME = "delivered_bot"

# A revoke deploy that is still running is left alone for an hour, which is what
# "the access comes back later" means here. The attempts budget is small so the
# give-up path can be reached without a hundred ticks.
_MAX_REVOKE_ATTEMPTS = 2
_CONFIG = {
    "supervisor.temporary_access_ttl_minutes": 600,
    "supervisor.temporary_access_revoke_stale_minutes": 60,
    "supervisor.temporary_access_max_revoke_attempts": _MAX_REVOKE_ATTEMPTS,
    "supervisor.temporary_access_observation_window_minutes": 0,
    # Zero: a grant that has not been handed back is past being a hiccup as soon
    # as its attempts run out, which is the give-up path these tests reach.
    "supervisor.temporary_access_unrevoked_ttl_minutes": 0,
    "supervisor.temporary_access_revoked_watch_minutes": 60,
    "supervisor.temporary_access_contract_audit_hours": 24,
}


@pytest.fixture
async def config(api_client):
    """Load the sweep's operational constants the way the service loads them."""
    from src import startup

    for key, value in _CONFIG.items():
        await api_client.request(
            "POST",
            "system-configs/",
            json={"key": key, "value": value, "category": "supervisor"},
        )
    store = ConfigStore(api_client.base_url, cache_ttl=0)
    previous = startup.config
    startup.config = store
    yield store
    startup.config = previous


class _Keys:
    """The little bit of Redis the sweep carries its question and answer in."""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(self.values.pop(key, None) is not None for key in keys)


@pytest.fixture
def redis_client():
    """Collects what the supervisor and the sweep publish, without a broker."""

    class _Collector:
        def __init__(self):
            self.published: list[tuple[str, object]] = []
            self.flat: list[tuple[str, dict]] = []
            self.redis = _Keys()

        async def publish_message(self, queue, message):
            self.published.append((queue, message))

        async def publish_flat(self, queue, fields):
            self.flat.append((queue, fields))

        def story_events(self, story_id: str) -> list[dict]:
            return [fields for _, fields in self.flat if fields.get("story_id") == story_id]

        def messages(self, queue: str, project_id: str) -> list:
            return [m for q, m in self.published if q == queue and m.project_id == project_id]

        def answer(self, request_id: str, *, present: bool) -> None:
            """Leave the answer the reader on the server would have left."""
            self.redis.values[env_observation_result_key(request_id)] = EnvObservationResult(
                request_id=request_id,
                outcome=EnvObservationOutcome.OBSERVED,
                env_key=ENV_KEY,
                present=present,
                containers=2,
            ).model_dump_json()

    return _Collector()


async def _delivered_story(api_client) -> tuple[str, str, str, str]:
    """A story in TESTING whose QA run passed, with the access still out.

    Returns the project, the application, the story and the QA run — the four
    identifiers everything after this is asserted against.
    """
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    await api_client.request(
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"owner_{telegram_id}"},
    )
    await api_client.request(
        "POST",
        "projects/",
        json={"id": project_id, "title": "Delivered Project", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )

    handle = f"vps-{uuid.uuid4().hex[:8]}"
    await api_client.request(
        "POST",
        "servers/",
        json={"handle": handle, "host": f"{handle}.example.com", "public_ip": "10.9.9.9"},
    )
    repo = await api_client.request(
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"repo-{project_id[:8]}",
            "git_url": f"https://github.com/test-org/repo-{project_id[:8]}.git",
        },
    )
    application = await api_client.request(
        "POST",
        "applications/",
        json={
            "repo_id": repo.json()["id"],
            "server_handle": handle,
            "service_name": "delivered-bot",
            "status": "running",
        },
    )
    application_id = application.json()["id"]

    story = await api_client.request(
        "POST",
        "stories/",
        json={"project_id": project_id, "title": "A bot that answers"},
    )
    story_id = story.json()["id"]
    for action in ("start", "deploy", "test"):
        await api_client.transition_story(story_id, action)

    qa_run_id = f"qa-{uuid.uuid4().hex[:8]}"
    qa_message = QAMessage(
        story_id=story_id,
        project_id=project_id,
        initiating_run_id="live-run-1",
        telegram_chat_id=str(telegram_id),
        deployed_url=DEPLOYED_URL,
        application_id=application_id,
        acceptance_criteria="the bot answers /start",
        bot_username=BOT_USERNAME,
        run_id=qa_run_id,
    )
    await api_client.create_run(
        {
            "id": qa_run_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                "application_id": application_id,
                QA_HANDOFF_KEY: QAHandoffPlan(qa_message=qa_message).model_dump(mode="json"),
            },
        }
    )
    # The QA worker's verdict on the product: it works.
    await api_client.update_run(
        qa_run_id,
        {
            "status": RunStatus.COMPLETED.value,
            "result": {
                "qa_outcome": QAOutcome.PASSED.value,
                "summary": "the bot answered",
                "deployed_url": DEPLOYED_URL,
            },
        },
    )

    # And the identity it borrowed is still out: the revoke deploy has been
    # dispatched and has not come back.
    revoke_run_id = f"deploy-revoke-{uuid.uuid4().hex[:8]}"
    await api_client.create_run({"id": revoke_run_id, "type": "deploy", "project_id": project_id})
    grant = await api_client.create_temporary_access_grant(
        TemporaryAccessGrantCreate(
            id=f"tempaccess-{qa_run_id}",
            project_id=project_id,
            env_key=ENV_KEY,
            subject="424242",
            head_sha=HEAD_SHA,
            qa_run_id=qa_run_id,
            grant_run_id=f"deploy-grant-{uuid.uuid4().hex[:8]}",
            qa_message=qa_message,
        )
    )
    await api_client.update_temporary_access_grant(
        grant.id,
        TemporaryAccessGrantUpdate(
            status=TemporaryAccessStatus.REVOKING,
            revoke_reason=TemporaryAccessRevokeReason.RUN_TERMINAL,
            revoke_run_id=revoke_run_id,
            revoke_attempts=1,
        ),
    )
    return project_id, story_id, qa_run_id, revoke_run_id


@pytest.mark.asyncio
async def test_a_passed_story_is_delivered_while_the_revoke_is_still_out(
    api_client, redis_client, config
):
    """The card, in one tick: the product does not wait for the cleanup.

    The grant is live and the revoke has not been confirmed by anything. That
    used to be the reason not to finish the story; now it is only the sweep's
    business, and the owner gets the address of the bot QA tested.
    """
    from src.tasks.supervisor import supervise_testing_stories

    project_id, story_id, qa_run_id, _ = await _delivered_story(api_client)

    counts = await supervise_testing_stories(api_client, redis_client)

    assert counts["completed"] >= 1
    story = await api_client.get_story(story_id)
    assert story.status == StoryStatus.COMPLETED.value

    events = redis_client.story_events(story_id)
    assert len(events) == 1
    assert events[0]["event"] == "story_completed"
    assert DEPLOYED_URL in events[0]["text"]
    assert BOT_USERNAME in events[0]["text"]
    assert events[0]["telegram_chat_id"]

    # And the access is exactly where it was: still out, still the sweep's.
    still_out = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert still_out is not None
    assert still_out.status is TemporaryAccessStatus.REVOKING


@pytest.mark.asyncio
async def test_the_cleanup_carries_on_after_the_story_was_delivered(
    api_client, redis_client, config
):
    """Delivery lets go of the cleanup; it does not stop it.

    The revoke lands after the story is finished, and the sweep does what it
    always did with it: reads the running service, keeps the grant open until
    the readings agree over the confirmation window, and asks again. The story
    stays completed through all of it and is told about once.
    """
    from src.tasks.supervisor import supervise_testing_stories
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, story_id, qa_run_id, revoke_run_id = await _delivered_story(api_client)
    await supervise_testing_stories(api_client, redis_client)

    # The revoke deploy comes back successful — the "ten minutes later" of the
    # scenario. It is a request that succeeded, not proof the value is gone.
    await api_client.update_run(
        revoke_run_id,
        {"status": RunStatus.COMPLETED.value, "result": {"deploy_outcome": "success"}},
    )

    await supervise_temporary_access(api_client, redis_client)
    asked = redis_client.messages(ENV_OBSERVATION_QUEUE, project_id)
    assert asked, "the sweep must go on reading the running service after delivery"

    redis_client.answer(asked[-1].request_id, present=False)
    counts = await supervise_temporary_access(api_client, redis_client)

    reconciling = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert reconciling is not None, "one clear reading is a moment, not a settled grant"
    assert reconciling.status is TemporaryAccessStatus.REVOKING
    assert reconciling.slot_clear_readings == 1
    # Still working on it, not given up on: the two are separate counts.
    assert counts["escalated"] == 0

    # Nothing about any of that reached the story or the owner a second time.
    story = await api_client.get_story(story_id)
    assert story.status == StoryStatus.COMPLETED.value
    assert len(redis_client.story_events(story_id)) == 1


@pytest.mark.asyncio
async def test_a_cleanup_that_gave_up_calls_a_human_and_leaves_the_story_alone(
    api_client, redis_client, config
):
    """The risk this card is really about: delivered early, cleanup lost.

    The revokes run out. An administrator is called by name — story, project,
    run and grant — while the completed QA verdict stays authoritative. The
    delivered story is not reopened, no engineering work is created from it,
    and the owner is not told a second time.
    """
    from src.tasks.supervisor import supervise_testing_stories
    from src.tasks.temporary_access import supervise_temporary_access

    project_id, story_id, qa_run_id, revoke_run_id = await _delivered_story(api_client)
    await supervise_testing_stories(api_client, redis_client)

    # The last revoke attempt failed, and there is no budget left for another.
    await api_client.update_run(
        revoke_run_id,
        {"status": RunStatus.FAILED.value, "result": {"deploy_outcome": "give_up"}},
    )
    await api_client.update_temporary_access_grant(
        f"tempaccess-{qa_run_id}",
        TemporaryAccessGrantUpdate(revoke_attempts=_MAX_REVOKE_ATTEMPTS),
    )

    with patch("src.tasks.temporary_access.notify_admins_best_effort", AsyncMock()) as alert:
        counts = await supervise_temporary_access(api_client, redis_client)

    assert counts["escalated"] == 1
    alerted = " ".join(str(call.args[0]) for call in alert.await_args_list)
    assert story_id in alerted
    assert project_id in alerted
    assert qa_run_id in alerted
    assert f"tempaccess-{qa_run_id}" in alerted

    # The grant says a human has been called without replacing the QA verdict.
    run = await api_client.get_run(qa_run_id)
    assert run.status is RunStatus.COMPLETED
    assert run.result.qa_outcome is QAOutcome.PASSED
    escalated = await api_client.get_live_temporary_access_grant_for_run(qa_run_id)
    assert escalated is not None
    assert escalated.escalated_at is not None

    # The delivered story is untouched by the incident, on this tick and the next.
    story = await api_client.get_story(story_id)
    assert story.status == StoryStatus.COMPLETED.value
    await supervise_testing_stories(api_client, redis_client)
    story = await api_client.get_story(story_id)
    assert story.status == StoryStatus.COMPLETED.value
    assert await api_client.get_tasks_by_story(story_id) == []
    assert redis_client.messages(ENGINEERING_QUEUE, project_id) == []
    assert len(redis_client.story_events(story_id)) == 1
    # Nothing was deployed on the story's behalf either — the only deploys here
    # are the sweep's own.
    assert all(
        message.env_overrides == {ENV_KEY: ""}
        for message in redis_client.messages(DEPLOY_QUEUE, project_id)
    )
