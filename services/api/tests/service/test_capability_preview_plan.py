"""Service proofs: a stored preview, the brief that relies on it, and the plan beside it.

Against the real database and routes: the platform stores a preview, a revision that
names it is opened only when its capabilities and answers resolve under it, the plan the
API derives is stored beside that revision and never shown in it, confirmation derives it
again and freezes exactly that plan, and every forged, foreign, stale, incomplete or
conflicting proposal is refused before any revision or plan exists.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.catalog_activation import CATALOG_ACTIVATION
from shared.models import ProductBrief

PREVIEWS = "/api/capability-previews/"
BRIEFS = "/api/product-briefs"
CHANNELS = "cap-5de1cd8d9a7c"


def _install() -> dict:
    return {
        "package": {
            "name": "tg-channels",
            "distribution": "codegen-kit-tg-channels",
            "version": "0.1.2",
            "tag": "packages/tg-channels/v0.1.2",
        },
        "libraries": [],
        "binding": {
            "package": "tg-channels",
            "resource": "codegen_kit_tg_channels:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": [],
        },
        "core_version": CATALOG_ACTIVATION.core_version,
        "python_version": "3.12.0",
        "catalog_digest": CATALOG_ACTIVATION.catalog_digest,
        "tooling_commit": CATALOG_ACTIVATION.tooling_commit,
        "catalog": CATALOG_ACTIVATION.source().model_dump(),
    }


def _preview_body(project_id: str) -> dict:
    language = {
        "question_id": "product_language",
        "kind": "product_language",
        "required": True,
        "choices": ["ru", "en"],
    }
    channels = {
        "question_id": "channels.q1",
        "kind": "text_list",
        "required": False,
        "max_items": 50,
        "item_pattern": "^[A-Za-z][A-Za-z0-9_]{3,31}$",
    }
    return {
        "project_id": project_id,
        "requests": [
            {"request_id": "channels", "capability_id": CHANNELS, "wording": "Cyprus channels"},
            {"request_id": "pay", "wording": "card payments"},
        ],
        "product": {
            "routes": [
                {
                    "request_id": "channels",
                    "capability_id": CHANNELS,
                    "route": "module",
                    "reason": "offered",
                },
                {"request_id": "pay", "route": "impossible", "reason": "platform_cannot"},
            ],
            "questions": [
                {**language, "request_ids": ["channels"]},
                {**channels, "request_ids": ["channels"]},
            ],
            "limitations": [{"request_id": "channels", "name": "channels_max", "value": 50}],
        },
        "technical": {
            "activation": CATALOG_ACTIVATION.model_dump(),
            "modules": [
                {"request_id": "channels", "capability_id": CHANNELS, "install": _install()}
            ],
            "targets": [
                {**language, "key": "language", "schema": {"type": "string", "enum": ["ru", "en"]}},
                {
                    **channels,
                    "key": "tg_channels.starting_channels",
                    "unique_items": True,
                    "schema": {"type": "array"},
                },
            ],
        },
    }


def _content(preview_id: str, language: str = "en", **changes) -> dict:
    capabilities = {
        "preview_id": preview_id,
        "capabilities": [
            {
                "request_id": "channels",
                "capability_id": CHANNELS,
                "route": "module",
                "requirement_ids": ["digest"],
            }
        ],
        "answers": [
            {
                "question_id": "product_language",
                "kind": "product_language",
                "value": language,
                "description": "The bot speaks the chosen language",
            },
            {
                "question_id": "channels.q1",
                "kind": "text_list",
                "value": ["durov"],
                "description": "Every user starts with @durov",
            },
        ],
    }
    return {
        "summary": "Digests of public Telegram channels",
        "must_requirements": [
            {
                "id": "digest",
                "text": "Shows a digest of my channels",
                "user_wording": "digest of Cyprus channels",
                "wording_reference": None,
                "user_facing": True,
            }
        ],
        "initial_settings": [],
        "language": "en",
        "usage_examples": [
            {"requirement_id": "digest", "user_sends": "/digest", "product_answers": "Latest posts"}
        ],
        "limitations": ["At most 50 channels"],
        "variant_choices": [],
        "capabilities": capabilities,
    } | changes


async def _owner(client: AsyncClient) -> int:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"cap-{telegram_id}"}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return telegram_id


async def _project(client: AsyncClient, telegram_id: int) -> str:
    project_id = str(uuid.uuid4())
    created = await client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Channel digests",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True, "modules": ["backend", "tg_bot"]},
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return project_id


async def _preview(client: AsyncClient, project_id: str) -> str:
    stored = await client.post(PREVIEWS, json=_preview_body(project_id))
    assert stored.status_code == HTTPStatus.CREATED, stored.text
    return stored.json()["preview_id"]


async def _create(client: AsyncClient, project_id: str, content: dict, **headers):
    return await client.post(
        f"{BRIEFS}/",
        headers=headers,
        json={
            "project_id": project_id,
            "title": "Channel digests",
            "content": content,
            "request_id": f"req-{uuid.uuid4().hex}",
        },
    )


async def _plan(client: AsyncClient, brief_id: str) -> dict:
    read = await client.get(f"{BRIEFS}/{brief_id}/capability-plan")
    assert read.status_code == HTTPStatus.OK, read.text
    return read.json()


@pytest.fixture
async def owned(async_client: AsyncClient) -> tuple[int, str, str]:
    telegram_id = await _owner(async_client)
    project_id = await _project(async_client, telegram_id)
    return telegram_id, project_id, await _preview(async_client, project_id)


def _refusal(response) -> dict:
    return response.json()["detail"]["capability_refusal"]


@pytest.mark.asyncio
async def test_only_the_platform_stores_a_preview(async_client: AsyncClient):
    telegram_id = await _owner(async_client)
    project_id = await _project(async_client, telegram_id)

    refused = await async_client.post(
        PREVIEWS, headers={"X-Telegram-ID": str(telegram_id)}, json=_preview_body(project_id)
    )
    assert refused.status_code == HTTPStatus.FORBIDDEN

    stored = await async_client.post(PREVIEWS, json=_preview_body(project_id))
    assert stored.status_code == HTTPStatus.CREATED, stored.text
    read = await async_client.get(
        f"{PREVIEWS.rstrip('/')}/{stored.json()['preview_id']}",
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert read.status_code == HTTPStatus.OK
    # The user reads the product projection; the technical half is not in it.
    assert "technical" not in read.json() and "tg-channels" not in read.text


@pytest.mark.asyncio
async def test_a_preview_from_another_activation_is_not_stored(async_client: AsyncClient):
    telegram_id = await _owner(async_client)
    project_id = await _project(async_client, telegram_id)
    body = _preview_body(project_id)
    body["technical"]["activation"]["commit"] = "0" * 40
    body["technical"]["modules"][0]["install"]["catalog"]["commit"] = "0" * 40

    stored = await async_client.post(PREVIEWS, json=body)

    assert stored.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert _refusal(stored)["code"] == "preview_stale"


@pytest.mark.asyncio
async def test_the_plan_is_derived_stored_beside_the_revision_and_frozen_by_confirmation(
    async_client: AsyncClient, owned
):
    telegram_id, project_id, preview_id = owned
    content = _content(preview_id)

    created = await _create(
        async_client, project_id, content, **{"X-Telegram-ID": str(telegram_id)}
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    brief_id = created.json()["id"]
    # The revision carries product intent only.
    assert created.json()["content"]["capabilities"] == content["capabilities"]
    assert "tg-channels" not in created.text and "catalog" not in created.text

    plan = await _plan(async_client, brief_id)
    assert (
        plan["preview_id"] == preview_id
        and plan["activation"]["commit"] == CATALOG_ACTIVATION.commit
    )
    [module] = plan["capabilities"]
    assert module["install"] == _install() and module["requirement_ids"] == ["digest"]
    assert [(s["key"], s["scope"], s["value"]) for s in plan["settings"]] == [
        ("language", "product", "en"),
        ("tg_channels.starting_channels", "product", ["durov"]),
    ]
    user_read = await async_client.get(
        f"{BRIEFS}/{brief_id}/capability-plan", headers={"X-Telegram-ID": str(telegram_id)}
    )
    assert user_read.status_code == HTTPStatus.FORBIDDEN

    confirm = {"request_id": f"conf-{uuid.uuid4().hex}", "content": content}
    confirmed = await async_client.post(f"{BRIEFS}/{brief_id}/confirm", json=confirm)
    assert confirmed.status_code == HTTPStatus.OK, confirmed.text
    replayed = await async_client.post(f"{BRIEFS}/{brief_id}/confirm", json=confirm)
    assert replayed.status_code == HTTPStatus.OK
    assert replayed.json()["confirmed_at"] == confirmed.json()["confirmed_at"]
    assert await _plan(async_client, brief_id) == plan


@pytest.mark.asyncio
async def test_a_corrected_answer_is_a_new_revision_with_its_own_plan(
    async_client: AsyncClient, owned
):
    _, project_id, preview_id = owned
    first = await _create(async_client, project_id, _content(preview_id, "en"))
    second = await _create(async_client, project_id, _content(preview_id, "ru"))

    assert first.status_code == second.status_code == HTTPStatus.CREATED
    assert second.json()["revision"] == first.json()["revision"] + 1
    first_plan = await _plan(async_client, first.json()["id"])
    second_plan = await _plan(async_client, second.json()["id"])
    assert first_plan["settings"][0]["value"] == "en"
    assert second_plan["settings"][0]["value"] == "ru"
    assert first_plan["capabilities"] == second_plan["capabilities"]


async def _brief_count(db_session: AsyncSession, project_id: str) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(ProductBrief)
        .where(ProductBrief.project_id == uuid.UUID(project_id))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,code",
    [
        (
            lambda c, p: c["capabilities"].update(preview_id="preview-" + "f" * 24),
            "preview_unknown",
        ),
        (lambda c, p: c["capabilities"]["answers"].pop(0), "missing_answer"),
        (
            lambda c, p: c["capabilities"]["answers"][0].update(value="de"),
            "invalid_answer",
        ),
        (
            lambda c, p: c["capabilities"]["answers"].append(
                {
                    "question_id": "channels.q7",
                    "kind": "choice",
                    "value": "x",
                    "description": "Invented",
                }
            ),
            "unknown_question",
        ),
        (
            lambda c, p: c["capabilities"]["capabilities"].append(
                {"request_id": "pay", "route": "from_scratch", "requirement_ids": ["digest"]}
            ),
            "impossible_capability",
        ),
        (
            lambda c, p: c["capabilities"]["capabilities"][0].update(route="from_scratch"),
            "capabilities_mismatch",
        ),
        (
            lambda c, p: c["initial_settings"].append(
                {"key": "language", "scope": "product", "value": "ru", "description": "Russian"}
            ),
            "setting_conflict",
        ),
    ],
    ids=["forged", "missing", "locale", "unknown", "impossible", "mismatch", "conflict"],
)
async def test_a_proposal_that_does_not_resolve_opens_nothing(
    async_client: AsyncClient, db_session: AsyncSession, owned, mutate, code
):
    _, project_id, preview_id = owned
    content = _content(preview_id)
    mutate(content, project_id)

    created = await _create(async_client, project_id, content)

    assert created.status_code == HTTPStatus.UNPROCESSABLE_CONTENT, created.text
    assert _refusal(created)["code"] == code
    assert await _brief_count(db_session, project_id) == 0


@pytest.mark.asyncio
async def test_a_preview_of_another_project_is_refused(
    async_client: AsyncClient, db_session: AsyncSession, owned
):
    telegram_id, project_id, _ = owned
    other = await _project(async_client, telegram_id)
    foreign = await _preview(async_client, other)

    created = await _create(async_client, project_id, _content(foreign))

    assert created.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert _refusal(created)["code"] == "preview_foreign"
    assert await _brief_count(db_session, project_id) == 0


@pytest.mark.asyncio
async def test_a_revision_is_not_confirmed_after_the_activation_moved(
    async_client: AsyncClient, owned, monkeypatch
):
    _, project_id, preview_id = owned
    content = _content(preview_id)
    created = await _create(async_client, project_id, content)
    brief_id = created.json()["id"]
    moved = CATALOG_ACTIVATION.model_copy(update={"commit": "1" * 40})
    monkeypatch.setattr("src.routers.capability_previews.CATALOG_ACTIVATION", moved)

    confirmed = await async_client.post(
        f"{BRIEFS}/{brief_id}/confirm",
        json={"request_id": f"conf-{uuid.uuid4().hex}", "content": content},
    )

    assert confirmed.status_code == HTTPStatus.CONFLICT
    assert _refusal(confirmed)["code"] == "preview_stale"
    read = await async_client.get(f"{BRIEFS}/{brief_id}")
    assert read.json()["confirmed_at"] is None


@pytest.mark.asyncio
async def test_concurrent_revisions_of_one_preview_each_keep_a_plan(
    async_client: AsyncClient, db_session: AsyncSession, owned
):
    _, project_id, preview_id = owned
    results = await asyncio.gather(
        *(
            _create(async_client, project_id, _content(preview_id, language))
            for language in ("en", "ru")
        )
    )

    statuses = sorted(result.status_code for result in results)
    assert statuses in (
        [HTTPStatus.CREATED, HTTPStatus.CREATED],
        [HTTPStatus.CREATED, HTTPStatus.CONFLICT],
    )
    rows = (
        await db_session.scalars(
            select(ProductBrief).where(ProductBrief.project_id == uuid.UUID(project_id))
        )
    ).all()
    assert rows and all(
        row.capability_plan and row.capability_preview_id == preview_id for row in rows
    )


@pytest.mark.asyncio
async def test_a_project_seeding_lookup_finds_a_brief_carrying_only_planned_answers(
    async_client: AsyncClient, owned
):
    _, project_id, preview_id = owned
    content = _content(preview_id)
    created = await _create(async_client, project_id, content)
    await async_client.post(
        f"{BRIEFS}/{created.json()['id']}/confirm",
        json={"request_id": f"conf-{uuid.uuid4().hex}", "content": content},
    )

    found = await async_client.get(f"{BRIEFS}/by-project/{project_id}/initial-settings")

    assert found.status_code == HTTPStatus.OK and found.json()["id"] == created.json()["id"]
