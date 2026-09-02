"""Unit tests for stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.queues.qa import QAMessage
from src.database import get_async_session
from src.main import app
from src.routers.stories import _completion_notification_text
from src.schemas.story import StoryAcceptance


def _make_story(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": "story-test1",
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "parent_story_id": None,
        "title": "Test story",
        "description": None,
        "acceptance_criteria": None,
        "type": "product",
        "status": "created",
        # The row carries the wait its status implies; the transition that
        # produced the status wrote it.
        "waiting_on": "none",
        "priority": 0,
        "blocked_by_story_id": None,
        "created_by": "system",
        "user_report": None,
        "quarantine_reason": None,
        "operator_acceptance": None,
        "operator_recheck": None,
        "reopened_at": None,
        "owner_notification": None,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    story = MagicMock()
    for k, v in defaults.items():
        setattr(story, k, v)
    return story


def _mock_session(scalar_one_or_none=None, scalars_all=None):
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    if scalars_all is not None:
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=scalars_all)
        mock_result.scalars = MagicMock(return_value=mock_scalars)

    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def _override_session(session):
    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override


# --- CRUD ---


@pytest.mark.asyncio
async def test_create_story():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/",
            json={"title": "User login", "project_id": "00000000-0000-0000-0000-000000000001"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    story = session.add.call_args[0][0]
    assert story.title == "User login"
    assert story.status == "created"
    assert story.project_id == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert story.id.startswith("story-")


@pytest.mark.asyncio
async def test_create_story_with_priority():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/",
            json={
                "title": "High prio",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "priority": 5,
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    story = session.add.call_args[0][0]
    assert story.priority == 5


@pytest.mark.asyncio
async def test_create_story_with_blocked_by():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/",
            json={
                "title": "Blocked",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "blocked_by_story_id": "story-dep",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    story = session.add.call_args[0][0]
    assert story.blocked_by_story_id == "story-dep"


@pytest.mark.asyncio
async def test_create_story_rejects_retry_of_qa_failure_held_parent():
    parent = _make_story(
        id="story-qa-held",
        status="waiting_human_review",
        quarantine_reason={"qa_failure": {"fingerprint": "qa-failure-123"}},
    )
    session = _mock_session(scalar_one_or_none=parent)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/",
            json={
                "title": "Retry login fix",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "parent_story_id": "story-qa-held",
            },
        )

    assert resp.status_code == HTTPStatus.CONFLICT
    assert resp.json()["detail"] == "Story story-qa-held requires human review before retrying"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_story_requires_project_id():
    session = _mock_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/", json={"title": "No project"})

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories():
    s1 = _make_story(id="story-1", title="First")
    s2 = _make_story(id="story-2", title="Second")
    session = _mock_session(scalars_all=[s1, s2])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/")

    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert len(data) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_project():
    s1 = _make_story(id="story-1", project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
    session = _mock_session(scalars_all=[s1])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/?project_id=00000000-0000-0000-0000-000000000001")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_status():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/?status=in_progress")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_filter_by_parent():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/?parent_story_id=story-epic")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_story():
    story = _make_story(id="story-abc")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/story-abc")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["id"] == "story-abc"


@pytest.mark.asyncio
async def test_get_story_not_found():
    session = _mock_session(scalar_one_or_none=None)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/story-nonexistent")

    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_story():
    story = _make_story(id="story-abc")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.patch("/api/stories/story-abc", json={"title": "Updated title"})

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.title == "Updated title"


# --- Action endpoints (status transitions) ---


@pytest.mark.asyncio
async def test_start_story():
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"


@pytest.mark.asyncio
async def test_deploying_story_enters_human_review():
    """The endpoint the supervisor escalates a refused deploy through.

    It is an action path, not a status value — posting `waiting_human_review`
    reaches no route at all — and it has to accept a story that is DEPLOYING.
    """
    story = _make_story(id="story-abc", status="deploying")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/human-review")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "waiting_human_review"


@pytest.mark.asyncio
async def test_start_story_invalid_transition():
    story = _make_story(id="story-abc", status="archived")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_complete_story():
    story = _make_story(id="story-abc", status="in_progress")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "completed"


@pytest.mark.asyncio
async def test_complete_story_refuses_waiting_human_review_without_acceptance_audit():
    story = _make_story(id="story-abc", status="waiting_human_review")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert story.status == "waiting_human_review"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_story_without_qa_owes_a_story_backed_notification_in_the_same_commit():
    story = _make_story(id="story-abc", status="in_progress")
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = None
    session = _mock_session()
    session.execute.side_effect = [story_result, qa_result]
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "completed"
    record = story.owner_notification
    assert record["event"] == "story_completed"
    assert record["story_id"] == "story-abc"
    assert record["terminal_status"] == "completed"
    assert record["state"] == "owed"
    assert (
        record["text"]
        == "The story is finished. Tell the user the good news that their product is ready."
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_story_keeps_the_address_verified_by_qa():
    story = _make_story(id="story-abc", status="testing")
    qa_run = MagicMock(
        id="qa-abc",
        result={"qa_outcome": "passed"},
        run_metadata={
            QA_HANDOFF_KEY: QAHandoffPlan(
                qa_message=QAMessage(
                    story_id="story-abc",
                    project_id="00000000-0000-0000-0000-000000000001",
                    initiating_run_id="deploy-abc",
                    telegram_chat_id="1",
                    deployed_url="https://verified.example.com",
                    application_id=42,
                    acceptance_criteria="works",
                    bot_username="verified_bot",
                    run_id="qa-abc",
                )
            ).model_dump(mode="json")
        },
    )
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = qa_run
    application_result = MagicMock()
    application_result.scalar_one_or_none.return_value = "running"
    session = _mock_session()
    session.execute.side_effect = [story_result, qa_result, application_result]
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    assert "https://verified.example.com" in story.owner_notification["text"]
    assert "@verified_bot" in story.owner_notification["text"]


@pytest.mark.asyncio
async def test_human_accepted_completion_keeps_current_qa_deploy_address():
    story = _make_story(
        id="story-abc",
        status="waiting_human_review",
        quarantine_reason={"qa_failure": {"fingerprint": "qa-failure-123"}},
    )
    qa_run = MagicMock(
        id="qa-abc",
        result={"qa_outcome": "failed"},
        run_metadata={
            QA_HANDOFF_KEY: QAHandoffPlan(
                qa_message=QAMessage(
                    story_id="story-abc",
                    project_id="00000000-0000-0000-0000-000000000001",
                    initiating_run_id="deploy-abc",
                    telegram_chat_id="1",
                    deployed_url="https://accepted.example.com",
                    application_id=42,
                    acceptance_criteria="works",
                    run_id="qa-abc",
                )
            ).model_dump(mode="json")
        },
    )
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = qa_run
    application_result = MagicMock()
    application_result.scalar_one_or_none.return_value = "running"
    session = _mock_session()
    session.execute.side_effect = [
        story_result,
        qa_result,
        application_result,
        qa_result,
        application_result,
    ]
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/story-abc/accept-result",
            json={"basis": "Verified the running deployment manually."},
            headers={"X-Admin-Console-Operator": "orchestrator-admin"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert "https://accepted.example.com" in story.owner_notification["text"]
    assert "operator accepted" in story.owner_notification["text"]
    assert story.operator_acceptance["actor"] == "admin_console:orchestrator-admin"
    assert story.operator_acceptance["overridden_quarantine_reason"] == {
        "qa_failure": {"fingerprint": "qa-failure-123"}
    }
    assert story.quarantine_reason is None


@pytest.mark.asyncio
async def test_human_acceptance_does_not_send_a_stopped_application_address():
    story = _make_story(id="story-abc", status="waiting_human_review")
    qa_run = MagicMock(
        id="qa-abc",
        result={"qa_outcome": "failed"},
        run_metadata={
            QA_HANDOFF_KEY: QAHandoffPlan(
                qa_message=QAMessage(
                    story_id="story-abc",
                    project_id="00000000-0000-0000-0000-000000000001",
                    initiating_run_id="deploy-abc",
                    telegram_chat_id="1",
                    deployed_url="https://stopped.example.com",
                    application_id=42,
                    acceptance_criteria="works",
                    run_id="qa-abc",
                )
            ).model_dump(mode="json")
        },
    )
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = qa_run
    application_result = MagicMock()
    application_result.scalar_one_or_none.return_value = "stopped"
    session = _mock_session()
    session.execute.side_effect = [qa_result, application_result]

    text = await _completion_notification_text(
        story,
        session,
        acceptance=StoryAcceptance(
            actor="admin_console:orchestrator-admin",
            basis="Verified the result manually.",
            accepted_at=datetime.now(UTC),
        ),
    )

    assert "operator accepted the result" in text
    assert "https://stopped.example.com" not in text


@pytest.mark.asyncio
async def test_completion_query_excludes_qa_runs_before_the_reopen():
    reopened_at = datetime.now(UTC)
    story = _make_story(id="story-abc", status="in_progress", reopened_at=reopened_at)
    session = _mock_session()

    await _completion_notification_text(story, session)

    query = str(session.execute.await_args.args[0])
    assert "runs.created_at >=" in query


@pytest.mark.asyncio
async def test_complete_story_with_corrupt_passed_qa_handoff_fails_fast():
    story = _make_story(id="story-abc", status="testing")
    qa_run = MagicMock(
        id="qa-abc",
        result={"qa_outcome": "passed"},
        run_metadata={},
    )
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = qa_run
    session = _mock_session()
    session.execute.side_effect = [story_result, qa_result]
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        with pytest.raises(KeyError):
            await client.post("/api/stories/story-abc/complete")


@pytest.mark.asyncio
async def test_complete_story_invalid_transition():
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_wait_user_secret_story():
    story = _make_story(id="story-abc", status="deploying")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/wait-user-secret")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "waiting_user_secret"


@pytest.mark.asyncio
async def test_wait_user_secret_story_invalid_transition():
    story = _make_story(id="story-abc", status="testing")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/wait-user-secret")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_archive_story():
    story = _make_story(id="story-abc", status="completed")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/archive")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "archived"


@pytest.mark.asyncio
async def test_archive_from_created():
    """Stories can be archived directly from created status."""
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/archive")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "archived"


# --- Priority filter + sort ---


@pytest.mark.asyncio
async def test_list_stories_filter_by_priority():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/?priority=3")

    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_stories_with_sort():
    session = _mock_session(scalars_all=[])
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get("/api/stories/?sort=-created_at")

    assert resp.status_code == 200  # noqa: PLR2004


# --- Blocked-by validation ---


@pytest.mark.asyncio
async def test_fail_story_from_in_progress():
    """Story in_progress can be failed."""
    story = _make_story(id="story-abc", status="in_progress")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/fail")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "failed"


@pytest.mark.asyncio
async def test_fail_story_from_created():
    """Story in created can be failed."""
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/fail")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "failed"


@pytest.mark.asyncio
async def test_fail_story_invalid_from_archived():
    """Archived story cannot be failed."""
    story = _make_story(id="story-abc", status="archived")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/fail")

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_start_story_blocked_by_incomplete():
    """Cannot start a story whose blocker is not completed."""
    blocker = _make_story(id="story-blocker", status="in_progress")
    story = _make_story(id="story-abc", status="created", blocked_by_story_id="story-blocker")

    call_count = 0
    mock_result_story = MagicMock()
    mock_result_story.scalar_one_or_none = MagicMock(return_value=story)
    mock_result_blocker = MagicMock()
    mock_result_blocker.scalar_one_or_none = MagicMock(return_value=blocker)

    session = AsyncMock()

    async def _execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_result_story
        return mock_result_blocker

    session.execute = AsyncMock(side_effect=_execute_side_effect)

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 422  # noqa: PLR2004
    assert "blocked by story" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_start_story_blocked_by_completed():
    """Can start a story whose blocker is completed."""
    blocker = _make_story(id="story-blocker", status="completed")
    story = _make_story(id="story-abc", status="created", blocked_by_story_id="story-blocker")

    call_count = 0
    mock_result_story = MagicMock()
    mock_result_story.scalar_one_or_none = MagicMock(return_value=story)
    mock_result_blocker = MagicMock()
    mock_result_blocker.scalar_one_or_none = MagicMock(return_value=blocker)

    session = AsyncMock()

    async def _execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_result_story
        return mock_result_blocker

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"


@pytest.mark.asyncio
async def test_start_story_no_blocker():
    """Can start a story with no blocked_by set."""
    story = _make_story(id="story-abc", status="created", blocked_by_story_id=None)
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/start")

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.status == "in_progress"


# --- Reopen ---


@pytest.mark.asyncio
async def test_reopen_story_from_completed():
    """Completed story can be reopened with user_report."""
    story = _make_story(id="story-abc", status="completed")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/story-abc/reopen",
            json={"user_report": "Images still broken on mobile", "actor": "po"},
        )

    assert resp.status_code == HTTPStatus.OK
    assert story.status == "reopened"
    assert story.user_report == "Images still broken on mobile"


@pytest.mark.asyncio
async def test_reopen_story_without_user_report():
    """Completed story can be reopened without user_report."""
    story = _make_story(id="story-abc", status="completed")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/reopen")

    assert resp.status_code == HTTPStatus.OK
    assert story.status == "reopened"
    assert story.user_report is None


@pytest.mark.asyncio
async def test_reopen_story_invalid_from_in_progress():
    """IN_PROGRESS story cannot be reopened (already in progress)."""
    story = _make_story(id="story-abc", status="in_progress")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post(
            "/api/stories/story-abc/reopen",
            json={"user_report": "Something wrong"},
        )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_test_story_from_deploying():
    """Deploying story can transition to testing."""
    story = _make_story(id="story-abc", status="deploying")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/test")

    assert resp.status_code == HTTPStatus.OK
    assert story.status == "testing"


@pytest.mark.asyncio
async def test_test_story_invalid_from_created():
    """Created story cannot transition directly to testing."""
    story = _make_story(id="story-abc", status="created")
    session = _mock_session(scalar_one_or_none=story)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/test")

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
