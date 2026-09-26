"""Unit tests for stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.product_brief import (
    MustRequirement,
    ProductBriefContent,
    UsageExample,
)
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
        "generated_product_timeline": None,
        "operator_acceptance": None,
        "operator_recheck": None,
        "unverified_decisions": [],
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
    session.execute.side_effect = [
        story_result,
        qa_result,
        application_result,
        _briefs_result([]),
    ]
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete")

    assert resp.status_code == 200  # noqa: PLR2004
    # A Telegram bot is reached by its username; the backend address is not for the user.
    assert "https://verified.example.com" not in story.owner_notification["text"]
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


_DEPLOYED_URL = "http://212.24.101.230:8042"


def _passed_qa_result(*, bot_username: str | None, outcome: str = "passed"):
    qa_run = MagicMock(
        id="qa-abc",
        result={"qa_outcome": outcome},
        run_metadata={
            QA_HANDOFF_KEY: QAHandoffPlan(
                qa_message=QAMessage(
                    story_id="story-abc",
                    project_id="00000000-0000-0000-0000-000000000001",
                    initiating_run_id="deploy-abc",
                    telegram_chat_id="1",
                    deployed_url=_DEPLOYED_URL,
                    application_id=42,
                    acceptance_criteria="works",
                    bot_username=bot_username,
                    run_id="qa-abc",
                )
            ).model_dump(mode="json")
        },
    )
    result = MagicMock()
    result.scalars.return_value.first.return_value = qa_run
    return result


def _running_application_result():
    result = MagicMock()
    result.scalar_one_or_none.return_value = "running"
    return result


def _briefs_result(briefs):
    result = MagicMock()
    result.scalars.return_value.all.return_value = briefs
    return result


def _brief(*, story_id, language="ru", examples=()):
    content = ProductBriefContent(
        summary="Бот проверяет палиндромы",
        must_requirements=[MustRequirement(id="palindrome", text="Проверять палиндромы")],
        language=language,
        usage_examples=[
            UsageExample(requirement_id="palindrome", user_sends=sends, product_answers=answers)
            for sends, answers in examples
        ],
    )
    return MagicMock(story_id=story_id, content=content.model_dump(mode="json"))


_RUSSIAN_EXAMPLES = (
    ("шалаш", "Да, «шалаш» — палиндром"),
    ("привет", "Нет, «привет» не палиндром"),
)


@pytest.mark.asyncio
async def test_bot_completion_carries_the_story_briefs_examples_and_no_backend_address():
    story = _make_story(id="story-abc", status="testing")
    session = _mock_session()
    session.execute.side_effect = [
        _passed_qa_result(bot_username="palindrome_bot"),
        _running_application_result(),
        _briefs_result(
            [
                _brief(story_id="story-newer", examples=(("abba", "yes"),), language="en"),
                _brief(story_id="story-abc", examples=_RUSSIAN_EXAMPLES),
            ]
        ),
    ]

    text = await _completion_notification_text(story, session)

    assert "QA passed" in text
    assert "@palindrome_bot" in text
    assert "(ru)" in text
    for sends, answers in _RUSSIAN_EXAMPLES:
        assert f"the user sends: {sends} → the bot answers: {answers}" in text
    assert "abba" not in text
    assert _DEPLOYED_URL not in text
    assert "212.24.101.230" not in text
    assert "8042" not in text


@pytest.mark.asyncio
async def test_bot_completion_without_its_own_examples_uses_the_latest_project_brief_with_some():
    story = _make_story(id="story-fix", status="testing", type="fix")
    session = _mock_session()
    session.execute.side_effect = [
        _passed_qa_result(bot_username="palindrome_bot"),
        _running_application_result(),
        _briefs_result(
            [
                _brief(story_id="story-legacy", examples=()),
                _brief(story_id="story-abc", examples=_RUSSIAN_EXAMPLES),
                _brief(story_id="story-older", examples=(("kayak", "yes"),), language="en"),
            ]
        ),
    ]

    text = await _completion_notification_text(story, session)

    assert "@palindrome_bot" in text
    assert "(ru)" in text
    assert "the user sends: шалаш → the bot answers: Да, «шалаш» — палиндром" in text
    assert "kayak" not in text
    assert _DEPLOYED_URL not in text


@pytest.mark.asyncio
async def test_bot_completion_without_any_exemplified_brief_points_to_start_and_help():
    story = _make_story(id="story-abc", status="testing")
    session = _mock_session()
    session.execute.side_effect = [
        _passed_qa_result(bot_username="palindrome_bot"),
        _running_application_result(),
        _briefs_result([_brief(story_id="story-abc", examples=())]),
    ]

    text = await _completion_notification_text(story, session)

    assert "@palindrome_bot" in text
    assert "/start" in text
    assert "/help" in text
    assert _DEPLOYED_URL not in text
    query = str(session.execute.await_args.args[0])
    assert "product_briefs.confirmed_at IS NOT NULL" in query


@pytest.mark.asyncio
async def test_operator_accepted_bot_completion_follows_the_same_rule():
    story = _make_story(id="story-abc", status="waiting_human_review")
    session = _mock_session()
    session.execute.side_effect = [
        _passed_qa_result(bot_username="palindrome_bot", outcome="failed"),
        _running_application_result(),
        _briefs_result([_brief(story_id="story-abc", examples=_RUSSIAN_EXAMPLES)]),
    ]

    text = await _completion_notification_text(
        story,
        session,
        acceptance=StoryAcceptance(
            actor="admin_console:orchestrator-admin",
            basis="Verified the result manually.",
            accepted_at=datetime.now(UTC),
        ),
    )

    assert "operator accepted the deployed result" in text
    assert "@palindrome_bot" in text
    assert "the user sends: привет → the bot answers: Нет, «привет» не палиндром" in text
    assert _DEPLOYED_URL not in text


@pytest.mark.asyncio
async def test_completion_of_a_product_without_a_bot_keeps_its_address():
    story = _make_story(id="story-abc", status="testing")
    session = _mock_session()
    session.execute.side_effect = [
        _passed_qa_result(bot_username=None),
        _running_application_result(),
    ]

    text = await _completion_notification_text(story, session)

    assert text == (
        "The story is finished: it is deployed and QA passed. Tell the user the good "
        f"news and give them the address: {_DEPLOYED_URL}"
    )


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


@pytest.mark.asyncio
async def test_a_qa_completion_owes_the_checks_that_ran_and_the_unverified_ones():
    """The story_completed record carries the completing run's facts, structured."""
    unverified = {
        "name": "criterion not verifiable by QA: - POST /api/transactions returns 201",
        "reason": "needs an HTTP write",
        "origin": "withheld",
    }
    story = _make_story(id="story-abc", status="testing")
    qa_run = MagicMock(
        id="qa-abc",
        type="qa",
        story_id="story-abc",
        status="completed",
        result={
            "qa_outcome": "passed",
            "passed_checks": ["GET /health returns 200"],
            "unverified_checks": [unverified],
        },
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
    session.get = AsyncMock(return_value=qa_run)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.post("/api/stories/story-abc/complete", json={"qa_run_id": "qa-abc"})

    assert resp.status_code == 200  # noqa: PLR2004
    assert story.owner_notification["event"] == "story_completed"
    assert story.owner_notification["qa_verification"] == {
        "qa_run_id": "qa-abc",
        "passed_checks": ["GET /health returns 200"],
        "unverified_checks": [unverified],
    }


@pytest.mark.asyncio
async def test_a_completion_no_qa_run_names_carries_no_qa_facts():
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
    assert story.owner_notification["qa_verification"] is None


# --- The user's answer to unverified checks ---

_UNVERIFIED = {
    "name": "Telegram: a reminder email arrives",
    "reason": "needs an email inbox",
    "origin": "executor",
}


def _answering_session(story, qa_run):
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    qa_result = MagicMock()
    qa_result.scalars.return_value.first.return_value = qa_run
    session = _mock_session()
    session.execute.side_effect = [story_result, qa_result]
    _override_session(session)
    return session


def _routed_qa_run(**result):
    return MagicMock(
        id="qa-abc",
        result={"qa_outcome": "passed", "passed_checks": ["GET /health returns 200"], **result},
    )


async def _answer(body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        return await client.post("/api/stories/story-abc/unverified-decisions", json=body)


@pytest.mark.asyncio
async def test_an_answer_is_recorded_against_the_last_routed_qa_run():
    story = _make_story(id="story-abc", status="completed")
    session = _answering_session(story, _routed_qa_run(unverified_checks=[_UNVERIFIED]))

    resp = await _answer(
        {
            "decision": "accept_unverified",
            "check_names": [_UNVERIFIED["name"]],
            "recorded_by": "po",
        }
    )

    assert resp.status_code == HTTPStatus.OK
    [recorded] = resp.json()["unverified_decisions"]
    assert recorded["decision"] == "accept_unverified"
    assert recorded["check_names"] == [_UNVERIFIED["name"]]
    assert recorded["qa_run_id"] == "qa-abc"
    assert recorded["recorded_by"] == "po"
    assert recorded["decided_at"]
    # Recorded and nothing else: the story stays where it was.
    assert story.status == "completed"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_later_answer_is_appended_never_written_over():
    earlier = {
        "decision": "change_requirement",
        "check_names": [_UNVERIFIED["name"]],
        "qa_run_id": "qa-abc",
        "decided_at": "2026-09-25T10:00:00Z",
        "recorded_by": "po",
    }
    story = _make_story(id="story-abc", status="completed", unverified_decisions=[earlier])
    _answering_session(story, _routed_qa_run(unverified_checks=[_UNVERIFIED]))

    resp = await _answer(
        {
            "decision": "accept_unverified",
            "check_names": [_UNVERIFIED["name"]],
            "recorded_by": "po",
        }
    )

    assert resp.status_code == HTTPStatus.OK
    decisions = resp.json()["unverified_decisions"]
    assert [d["decision"] for d in decisions] == ["change_requirement", "accept_unverified"]
    assert decisions[0]["decided_at"].startswith("2026-09-25T10:00:00")


@pytest.mark.asyncio
async def test_a_check_the_run_did_not_leave_unverified_is_refused():
    story = _make_story(id="story-abc", status="completed")
    session = _answering_session(story, _routed_qa_run(unverified_checks=[_UNVERIFIED]))

    resp = await _answer(
        {"decision": "accept_unverified", "check_names": ["invented"], "recorded_by": "po"}
    )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "invented" in resp.json()["detail"]
    assert story.unverified_decisions == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_story_without_a_routed_qa_run_has_nothing_to_answer():
    story = _make_story(id="story-abc", status="in_progress")
    session = _answering_session(story, None)

    resp = await _answer(
        {"decision": "accept_unverified", "check_names": ["anything"], "recorded_by": "po"}
    )

    assert resp.status_code == HTTPStatus.CONFLICT
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_decision_is_refused_before_the_story_is_read():
    session = _mock_session()
    _override_session(session)

    resp = await _answer({"decision": "rerun", "check_names": ["x"], "recorded_by": "po"})

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    session.execute.assert_not_awaited()
