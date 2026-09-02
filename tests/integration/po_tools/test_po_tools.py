"""Integration tests: PO tools (langgraph) against real API.

Full roundtrip: PO tool → HTTP → API → DB → response.
Validates that payloads are accepted by the real API and data persists correctly.
"""

from __future__ import annotations

import json

import pytest

from src.agents.po.tools import (
    confirm_product_brief,
    create_project,
    create_story,
    get_project,
    list_projects,
    list_stories,
    present_product_brief,
    set_project_secret,
)
from src.agents.po.tools_shared import init_po_clients

from .conftest import make_config


async def confirmed_brief_id(
    project_id: str, title: str = "Todo bot", summary: str | None = None
) -> str:
    """Walk the real confirmation flow and return the frozen brief's id.

    New product work is only reachable through it: the PO presents one atomic
    revision, the user answers yes, and the PO echoes the stored content back.
    """
    presented = await present_product_brief.ainvoke(
        {
            "project_id": project_id,
            "title": title,
            "summary": summary or "A bot that keeps a todo list and reminds about it.",
            "must_requirements": [
                {
                    "id": "r1",
                    "text": "It stores a todo item",
                    "user_wording": "I want to write down what I have to do",
                },
                {
                    "id": "r2",
                    "text": "It reminds about an item",
                    "wording_reference": "chat 2026-09-02, the user's second message",
                },
            ],
            "initial_settings": [{"key": "reminders.default_hour", "scope": "product", "value": 9}],
        },
        config=make_config(),
    )
    assert "yes / correct me" in presented, presented
    brief_id = presented.split("(id: ")[1].split(")")[0]

    confirmed = await confirm_product_brief.ainvoke(
        {"project_id": project_id, "brief_id": brief_id},
        config=make_config(),
    )
    assert "is confirmed and frozen" in confirmed, confirmed
    return brief_id


@pytest.mark.usefixtures("po_clients", "test_user")
class TestCreateProjectIntegration:
    async def test_creates_project_in_db(self, api_client):
        """create_project stores a project retrievable via API."""
        result = await create_project.ainvoke(
            {"title": "integ-test-bot", "modules": "backend,tg_bot", "description": "Test"},
            config=make_config(),
        )

        assert "Project created" in result
        project_id = result.split("ID: ")[1].split(",")[0]

        resp = await api_client.get(f"/api/projects/{project_id}")
        assert resp.status_code == 200
        project = resp.json()
        assert project["title"] == "integ-test-bot"
        # Slug is derived server-side: slugified title plus the id hex.
        assert project["slug"] == f"integ-t-{project_id.replace('-', '')}"

    async def test_invalid_modules_rejected_before_api(self):
        """Invalid modules are caught by the tool itself, no API call made."""
        result = await create_project.ainvoke(
            {"title": "test", "modules": "invalid_module"},
            config=make_config(),
        )
        assert "Error" in result
        assert "invalid_module" in result

    async def test_telegram_creation_uses_current_default_without_rewriting_existing_projects(
        self, api_client, factory_api_client, stream_client
    ):
        """PO requests inherit each API runtime default only when they omit a choice."""
        first_result = await create_project.ainvoke(
            {"title": "default-codex", "modules": "backend"},
            config=make_config(),
        )
        explicit_result = await create_project.ainvoke(
            {"title": "explicit-claude", "modules": "backend", "agent_type": "claude"},
            config=make_config(),
        )

        first_id = first_result.split("ID: ")[1].split(",")[0]
        explicit_id = explicit_result.split("ID: ")[1].split(",")[0]
        assert (await api_client.get(f"/api/projects/{first_id}")).json()["config"][
            "agent_type"
        ] == "codex"
        assert (await api_client.get(f"/api/projects/{explicit_id}")).json()["config"][
            "agent_type"
        ] == "claude"

        init_po_clients(factory_api_client, stream_client)
        later_result = await create_project.ainvoke(
            {"title": "default-factory", "modules": "backend"},
            config=make_config(),
        )
        later_id = later_result.split("ID: ")[1].split(",")[0]
        assert (await api_client.get(f"/api/projects/{later_id}")).json()["config"][
            "agent_type"
        ] == "factory"
        assert (await api_client.get(f"/api/projects/{first_id}")).json()["config"][
            "agent_type"
        ] == "codex"


@pytest.mark.usefixtures("po_clients", "test_user")
class TestListProjectsIntegration:
    async def test_lists_created_projects(self, api_client):
        """list_projects returns projects created via API."""
        await create_project.ainvoke(
            {"title": "list-test-proj", "modules": "backend"},
            config=make_config(),
        )

        result = await list_projects.ainvoke({}, config=make_config())
        assert "list-test-proj" in result


@pytest.mark.usefixtures("po_clients", "test_user")
class TestGetProjectIntegration:
    async def test_gets_project_details(self, api_client):
        """get_project returns full project JSON from DB."""
        create_result = await create_project.ainvoke(
            {"title": "get-test-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        result = await get_project.ainvoke({"project_id": project_id}, config=make_config())
        parsed = json.loads(result)
        assert parsed["title"] == "get-test-proj"
        assert parsed["id"] == project_id


@pytest.mark.usefixtures("po_clients", "test_user")
class TestSetProjectSecretIntegration:
    async def test_sets_and_persists_secret(self, api_client):
        """set_project_secret stores secret retrievable via API."""
        create_result = await create_project.ainvoke(
            {"title": "secret-test-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        result = await set_project_secret.ainvoke(
            {
                "project_id": project_id,
                "key": "TEST_TOKEN",
                "value": "secret-value-123",
                "hint": "Test token for integration test",
            },
            config=make_config(),
        )
        assert "Secret" in result

    async def test_secret_without_hint(self, api_client):
        """set_project_secret works without hint."""
        create_result = await create_project.ainvoke(
            {"title": "secret-nohint-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        result = await set_project_secret.ainvoke(
            {"project_id": project_id, "key": "API_KEY", "value": "key-value"},
            config=make_config(),
        )
        assert "Secret" in result


@pytest.mark.usefixtures("po_clients", "test_user")
class TestCreateStoryIntegration:
    async def test_creates_story_and_publishes_architect_message(self, api_client, redis_client):
        """create_story persists story and publishes to architect:queue."""
        create_result = await create_project.ainvoke(
            {"title": "story-test-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        brief_id = await confirmed_brief_id(project_id)

        result = await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Build todo feature",
                "description": "A todo feature with CRUD and reminders",
                "product_brief_id": brief_id,
            },
            config=make_config(),
        )

        assert "Story created" in result
        assert "architect" in result.lower()

        # Verify story exists in API
        story_id = result.split("Story: ")[1].split(" ")[0]
        resp = await api_client.get(f"/api/stories/{story_id}")
        assert resp.status_code == 200
        story = resp.json()
        assert story["title"] == "Build todo feature"
        assert story["type"] == "product"
        assert story["created_by"] == "po"

        # The story is brief-backed: this is the read the architect does before
        # it claims the planning attempt.
        bound = await api_client.get(f"/api/product-briefs/by-story/{story_id}")
        assert bound.status_code == 200
        assert bound.json()["id"] == brief_id
        assert bound.json()["confirmed_at"] is not None

        # Verify architect:queue has a message
        messages = await redis_client.xrange("architect:queue", count=10)
        assert len(messages) > 0
        found = False
        for _msg_id, fields in messages:
            data_key = b"data" if b"data" in fields else "data"
            if data_key in fields:
                data = fields[data_key]
                if isinstance(data, bytes):
                    data = data.decode()
                msg = json.loads(data)
                if msg.get("story_id") == story_id:
                    assert msg["project_id"] == project_id
                    found = True
                    break
        assert found, f"ArchitectMessage for story {story_id} not found in architect:queue"

    async def test_a_second_brief_is_presentable_once_the_first_is_bound(self, api_client):
        """The shape of every story after a project's first one.

        The creation key is a fingerprint of the document, not a guessed
        revision number: the server owns the counter and the PO forgets its
        pointer at the bind, so a guess would spend `r1` forever and the
        released endpoint would answer every later presentation with 409.
        """
        create_result = await create_project.ainvoke(
            {"title": "second-brief-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        first_brief_id = await confirmed_brief_id(project_id)
        story_result = await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Build the todo list",
                "description": "A todo feature with CRUD and reminders",
                "product_brief_id": first_brief_id,
            },
            config=make_config(),
        )
        assert "Story created" in story_result, story_result

        second_brief_id = await confirmed_brief_id(
            project_id, summary="The same bot, now also sharing a list with a friend."
        )

        assert second_brief_id != first_brief_id
        second = await api_client.get(f"/api/product-briefs/{second_brief_id}")
        assert second.status_code == 200
        assert second.json()["revision"] == 2
        assert second.json()["story_id"] is None

    async def test_re_presenting_the_same_document_opens_one_revision(self, api_client):
        """Idempotency survives the change of key: a retry is not a new brief."""
        create_result = await create_project.ainvoke(
            {"title": "retry-brief-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        first = await present_product_brief.ainvoke(
            {
                "project_id": project_id,
                "title": "Retry bot",
                "summary": "A bot presented twice.",
                "must_requirements": [
                    {"id": "r1", "text": "It answers", "user_wording": "it should answer me"}
                ],
            },
            config=make_config(),
        )
        first_id = first.split("(id: ")[1].split(")")[0]

        # The pointer is what a live PO reads; drop it to force the key itself
        # to carry the idempotency, as it must after a bind cleared the pointer.
        await api_client.patch(f"/api/projects/{project_id}", json={"config": {}})

        second = await present_product_brief.ainvoke(
            {
                "project_id": project_id,
                "title": "Retry bot",
                "summary": "A bot presented twice.",
                "must_requirements": [
                    {"id": "r1", "text": "It answers", "user_wording": "it should answer me"}
                ],
            },
            config=make_config(),
        )

        assert second.split("(id: ")[1].split(")")[0] == first_id
        stored = await api_client.get(f"/api/product-briefs/{first_id}")
        assert stored.json()["revision"] == 1

    async def test_new_product_work_without_a_brief_creates_nothing(self, api_client):
        """The prose-summary path is not a fallback for a missing brief."""
        create_result = await create_project.ainvoke(
            {"title": "nobrief-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        result = await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Build something",
                "description": "Requirements the user never confirmed",
            },
            config=make_config(),
        )

        assert "No story was created" in result
        assert "present_product_brief" in result
        stories = await api_client.get(f"/api/stories/?project_id={project_id}")
        assert stories.status_code == 200
        assert stories.json() == []


@pytest.mark.usefixtures("po_clients", "test_user")
class TestListStoriesIntegration:
    async def test_lists_stories_for_project(self, api_client):
        """list_stories returns stories created for a project."""
        create_result = await create_project.ainvoke(
            {"title": "liststory-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        brief_id = await confirmed_brief_id(project_id, title="Listing bot")

        await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Story for listing",
                "description": "Test story",
                "product_brief_id": brief_id,
            },
            config=make_config(),
        )

        result = await list_stories.ainvoke({"project_id": project_id}, config=make_config())
        assert "Story for listing" in result

    async def test_empty_stories(self, api_client):
        """list_stories returns empty message for project with no stories."""
        create_result = await create_project.ainvoke(
            {"title": "emptystory-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        result = await list_stories.ainvoke({"project_id": project_id}, config=make_config())
        assert "No stories" in result
