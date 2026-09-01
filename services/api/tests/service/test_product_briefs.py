"""Service coverage for the durable Product Brief admission and coverage boundary."""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest


def _brief_content() -> dict:
    return {
        "intended_users": ["Russian and English speaking customers"],
        "languages": ["ru", "en"],
        "must_requirements": [
            {
                "id": "must-russian",
                "text": "The product must support Russian.",
                "source": "Пользователю нужен русский язык.",
            },
            {
                "id": "must-english",
                "text": "The product must support English.",
                "source": "The user also requires English.",
            },
        ],
        "initial_settings": [
            {"key": "languages", "value": ["ru", "en"], "scope": "product"},
        ],
    }


async def _create_owned_project(client: AsyncClient) -> tuple[str, str, str]:
    owner = str(810000000 + uuid.uuid4().int % 10000000)
    intruder = str(820000000 + uuid.uuid4().int % 10000000)
    for telegram_id in (owner, intruder):
        response = await client.post(
            "/api/users/",
            json={
                "telegram_id": int(telegram_id),
                "username": f"product-brief-{telegram_id}",
                "first_name": "Product Brief",
            },
        )
        assert response.status_code == HTTPStatus.CREATED, response.text

    project_id = str(uuid.uuid4())
    response = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"Product Brief {project_id[:8]}",
            "initiating_run_id": f"product-brief-{project_id[:8]}",
            "status": "active",
            "config": {},
        },
        headers={"X-Telegram-ID": owner},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return project_id, owner, intruder


async def _create_confirmed_brief(
    client: AsyncClient, project_id: str, owner: str
) -> tuple[str, dict]:
    content = _brief_content()
    request_id = f"brief-create-{uuid.uuid4()}"
    response = await client.post(
        "/api/product-briefs/",
        json={
            "project_id": project_id,
            "title": "Bilingual customer product",
            "content": content,
            "request_id": request_id,
        },
        headers={"X-Telegram-ID": owner},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    brief = response.json()
    assert brief["content"]["initial_settings"] == [
        {"key": "languages", "value": ["ru", "en"], "scope": "product", "subject": None}
    ]

    confirmation_request_id = f"brief-confirm-{uuid.uuid4()}"
    response = await client.post(
        f"/api/product-briefs/{brief['id']}/confirm",
        json={"request_id": confirmation_request_id, "content": content},
        headers={"X-Telegram-ID": owner},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    return brief["id"], {"request_id": confirmation_request_id, "content": content}


@pytest.mark.asyncio
async def test_product_brief_confirmation_coverage_and_story_gate(async_client: AsyncClient):
    project_id, owner, intruder = await _create_owned_project(async_client)
    brief_id, confirmation = await _create_confirmed_brief(async_client, project_id, owner)

    denied = await async_client.get(
        f"/api/product-briefs/{brief_id}", headers={"X-Telegram-ID": intruder}
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN, denied.text

    retry = await async_client.post(
        f"/api/product-briefs/{brief_id}/confirm",
        json=confirmation,
        headers={"X-Telegram-ID": owner},
    )
    assert retry.status_code == HTTPStatus.OK, retry.text
    assert retry.json()["id"] == brief_id

    mismatched = await async_client.post(
        f"/api/product-briefs/{brief_id}/confirm",
        json={
            "request_id": confirmation["request_id"],
            "content": {**confirmation["content"], "languages": ["ru"]},
        },
        headers={"X-Telegram-ID": owner},
    )
    assert mismatched.status_code == HTTPStatus.CONFLICT, mismatched.text

    story = await async_client.post(
        "/api/stories/",
        json={
            "project_id": project_id,
            "title": "Bilingual product story",
            "acceptance_criteria": "Architect-authored criteria are green.",
            "type": "product",
            "product_brief_id": brief_id,
        },
    )
    assert story.status_code == HTTPStatus.CREATED, story.text
    story_id = story.json()["id"]

    blocked = await async_client.post(f"/api/stories/{story_id}/start")
    assert blocked.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, blocked.text
    assert blocked.json()["detail"] == {
        "missing_product_brief_coverage": ["must-english", "must-russian"]
    }

    coverage = await async_client.put(
        f"/api/product-briefs/{brief_id}/coverage/must-russian",
        json={
            "requirement_id": "must-russian",
            "repository_acceptance_contract": (
                "Russian is available in the product language picker."
            ),
        },
    )
    assert coverage.status_code == HTTPStatus.OK, coverage.text

    returned = await async_client.put(
        f"/api/product-briefs/{brief_id}/coverage/must-english",
        json={
            "requirement_id": "must-english",
            "returned_reason": "English is returned to the PO for a later product revision.",
        },
    )
    assert returned.status_code == HTTPStatus.OK, returned.text
    assert returned.json()["returned_reason"] == (
        "English is returned to the PO for a later product revision."
    )

    started = await async_client.post(f"/api/stories/{story_id}/start")
    assert started.status_code == HTTPStatus.OK, started.text
    assert started.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_incomplete_brief_can_be_sent_to_architect_for_coverage(async_client: AsyncClient):
    project_id, owner, _intruder = await _create_owned_project(async_client)
    brief_id, _confirmation = await _create_confirmed_brief(async_client, project_id, owner)
    story = await async_client.post(
        "/api/stories/",
        json={
            "project_id": project_id,
            "title": "Story awaiting architect coverage",
            "type": "product",
            "product_brief_id": brief_id,
        },
    )
    assert story.status_code == HTTPStatus.CREATED, story.text

    dispatched = await async_client.post(
        f"/api/stories/{story.json()['id']}/send-to-architect", json={"actor": "operator"}
    )
    assert dispatched.status_code == HTTPStatus.OK, dispatched.text
    assert dispatched.json()["status"] == "in_progress"
