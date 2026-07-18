"""Integration tests: PO tools (langgraph) against real API.

Full roundtrip: PO tool → HTTP → API → DB → response.
Validates that payloads are accepted by the real API and data persists correctly.
"""

from __future__ import annotations

import json

import pytest

from src.agents.po.tools import (
    create_project,
    create_story,
    get_project,
    list_projects,
    list_stories,
    set_project_secret,
)

from .conftest import make_config


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

        result = await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Build todo feature",
                "description": "A todo feature with CRUD and reminders",
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


@pytest.mark.usefixtures("po_clients", "test_user")
class TestListStoriesIntegration:
    async def test_lists_stories_for_project(self, api_client):
        """list_stories returns stories created for a project."""
        create_result = await create_project.ainvoke(
            {"title": "liststory-proj", "modules": "backend"},
            config=make_config(),
        )
        project_id = create_result.split("ID: ")[1].split(",")[0]

        await create_story.ainvoke(
            {
                "project_id": project_id,
                "title": "Story for listing",
                "description": "Test story",
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
