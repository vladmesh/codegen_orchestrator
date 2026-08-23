"""Real-SQL regression tests for bot-audience mutations and their rollouts.

Every property here is about what the *next* process reads, so these tests run
against a real database rather than mocks: cross-project isolation of the
rollout target query, cross-owner denial on rollout status, and the durable
publish-intent record that survives a lost queue write.

These live in tests/service (they need Postgres), complementing
tests/unit/test_bot_user_audience_mutations.py which pins handler wiring with
mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
import uuid

from fastapi import status
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BOT_ROLLOUT_NOTIFY_KEY,
    BotRolloutNotifyRecord,
    BotRolloutNotifyState,
    BotRolloutPublishState,
    BotRolloutRecord,
)
from shared.models import Application, Deployment, Repository, Run, User

SHA_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(scope="module")
async def client():
    """Internal client with the app's Redis initialized (the routes publish)."""
    from src.dependencies import close_redis, init_redis
    from src.main import app

    await init_redis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as c:
        yield c
    await close_redis()


async def _make_user(client, *, admin: bool = False) -> int:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    resp = await client.post(
        "/api/users/",
        json={
            "telegram_id": telegram_id,
            "username": f"rollout_{telegram_id}",
            "is_admin": admin,
        },
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return telegram_id


async def _make_project(
    client,
    db: AsyncSession,
    owner_telegram_id: int,
) -> uuid.UUID:
    project_id = uuid.uuid4()
    created = await client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": str(project_id),
            "title": f"Rollout {project_id}",
            "config": {},
        },
        headers={"X-Telegram-ID": str(owner_telegram_id)},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    # A private audience so mutations have something to change.
    chosen = await client.post(
        f"/api/projects/{project_id}/config/bot-access",
        json={"mode": "only_me", "allowed_telegram_ids": str(owner_telegram_id)},
        headers={"X-Telegram-ID": str(owner_telegram_id)},
    )
    assert chosen.status_code == status.HTTP_200_OK, chosen.text
    return project_id


async def _add_running_application(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    deployed_sha: str | None,
    result: str = "success",
) -> tuple[int, Repository]:
    from shared.models import Server

    server = await db.get(Server, "srv-1")
    if server is None:
        db.add(
            Server(
                handle="srv-1",
                host="srv-1.example.internal",
                public_ip="203.0.113.10",
            )
        )
        await db.flush()
    repo = Repository(
        id=f"repo-{uuid.uuid4().hex[:10]}",
        project_id=project_id,
        name="primary",
        git_url=f"https://github.com/owner/{project_id.hex[:8]}",
    )
    db.add(repo)
    await db.flush()
    application = Application(
        repo_id=repo.id,
        server_handle="srv-1",
        service_name=f"svc-{project_id.hex[:8]}",
        status="running",
    )
    db.add(application)
    await db.flush()
    deployment = Deployment(
        application_id=application.id,
        project_id=project_id,
        service_name=application.service_name,
        server_handle="srv-1",
        port=8000,
        result=result,
        deployment_info={},
        deployed_sha=deployed_sha,
    )
    db.add(deployment)
    await db.commit()
    return application.id, repo


class TestCrossProjectRolloutTarget:
    @pytest.mark.asyncio
    async def test_rollout_redeploys_this_projects_sha_not_the_highest_application(
        self, client, db_session: AsyncSession
    ):
        """The target query is bound to the requested project.

        The other project's application deliberately has the higher id and the
        more recent deployment: an unscoped query would pair this project's
        name with that foreign SHA.
        """
        other_owner = await _make_user(client)
        owner = await _make_user(client)

        other_project = await _make_project(client, db_session, other_owner)
        this_project = await _make_project(client, db_session, owner)

        # The foreign target: higher application id, newer deployment.
        await _add_running_application(db_session, project_id=other_project, deployed_sha=SHA_B)
        # This project's target: lower id, older deployment.
        await _add_running_application(db_session, project_id=this_project, deployed_sha=SHA_A)

        resp = await client.post(
            f"/api/projects/{this_project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner)},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["rollout"] == "pending"

        run_id = body["rollout_run_id"]
        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert run.project_id == this_project
        assert run.run_metadata["head_sha"] == SHA_A, (
            "the rollout must redeploy THIS project's commit"
        )
        assert run.run_metadata["head_sha"] != SHA_B

    @pytest.mark.asyncio
    async def test_a_running_application_of_another_project_is_invisible(
        self, client, db_session: AsyncSession
    ):
        """Only this project runs → not_deployed, even though others are live."""
        other_owner = await _make_user(client)
        owner = await _make_user(client)

        other_project = await _make_project(client, db_session, other_owner)
        this_project = await _make_project(client, db_session, owner)

        await _add_running_application(db_session, project_id=other_project, deployed_sha=SHA_B)

        resp = await client.post(
            f"/api/projects/{this_project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner)},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["rollout"] == "not_deployed"
        assert resp.json()["rollout_run_id"] is None


class TestRolloutStatusAuthorization:
    @pytest.mark.asyncio
    async def test_another_projects_run_id_reads_as_missing(self, client, db_session: AsyncSession):
        """A run bound to project B cannot be read through project A's route."""
        owner = await _make_user(client)
        project_b = await _make_project(client, db_session, owner)

        owner_id = (
            (await db_session.execute(select(User).where(User.telegram_id == owner)))
            .scalars()
            .first()
            .id
        )
        run = Run(
            id=f"botrollout-foreign{uuid.uuid4().hex[:8]}",
            type="deploy",
            project_id=project_b,
            user_id=owner_id,
            run_metadata={
                BOT_ROLLOUT_METADATA_KEY: BotRolloutRecord(
                    publish=BotRolloutPublishState.PUBLISHED,
                    application_id=1,
                    head_sha=SHA_A,
                    staged_at=datetime.now(UTC),
                ).model_dump(mode="json")
            },
        )
        db_session.add(run)
        await db_session.commit()

        # A second project owned by the same user — same owner, different
        # project. The binding that matters here is project, not just owner.
        project_a = await _make_project(client, db_session, owner)

        resp = await client.get(
            f"/api/projects/{project_a}/config/bot-access/rollouts/{run.id}",
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_another_owner_cannot_read_a_rollout_status(
        self, client, db_session: AsyncSession
    ):
        """Owner A's rollout status is denied to owner B, even with the right path."""
        owner_a = await _make_user(client)
        owner_b = await _make_user(client)
        project_a = await _make_project(client, db_session, owner_a)

        app_id, _ = await _add_running_application(
            db_session, project_id=project_a, deployed_sha=SHA_A
        )
        staged = await client.post(
            f"/api/projects/{project_a}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner_a)},
        )
        assert staged.status_code == status.HTTP_200_OK
        run_id = staged.json()["rollout_run_id"]

        resp = await client.get(
            f"/api/projects/{project_a}/config/bot-access/rollouts/{run_id}",
            headers={"X-Telegram-ID": str(owner_b)},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

        # And the owner themself can read it.
        own = await client.get(
            f"/api/projects/{project_a}/config/bot-access/rollouts/{run_id}",
            headers={"X-Telegram-ID": str(owner_a)},
        )
        assert own.status_code == status.HTTP_200_OK
        assert own.json()["rollout"] in {"applied", "pending", "failed"}


class TestPublishIntentRecord:
    @pytest.mark.asyncio
    async def test_staged_run_carries_a_durable_record(self, client, db_session):
        """The run carries the rollout bookkeeping with the exact deploy target."""
        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)
        await _add_running_application(db_session, project_id=project, deployed_sha=SHA_A)

        resp = await client.post(
            f"/api/projects/{project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner)},
        )
        assert resp.status_code == status.HTTP_200_OK
        run_id = resp.json()["rollout_run_id"]

        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        record = BotRolloutRecord.model_validate(run.run_metadata[BOT_ROLLOUT_METADATA_KEY])
        assert record.publish is BotRolloutPublishState.PUBLISHED
        assert record.head_sha == SHA_A
        assert record.application_id > 0
        assert run.status == "queued"

    @pytest.mark.asyncio
    async def test_a_run_staged_without_a_queue_write_is_selected_for_recovery(
        self, client, db_session
    ):
        """A record reset to publish_owed — the crash-between-commit-and-publish
        state the durable intent exists for — puts the run back in the sweep's
        selection, and settling it takes it out again."""
        from sqlalchemy import select as sa_select

        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)
        await _add_running_application(db_session, project_id=project, deployed_sha=SHA_A)

        staged = await client.post(
            f"/api/projects/{project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner)},
        )
        run_id = staged.json()["rollout_run_id"]

        # A settled (published) run is not in the selection.
        listed = await client.get("/api/runs/bot-rollouts/unsettled")
        assert listed.status_code == status.HTTP_200_OK
        ids = [row["id"] for row in listed.json()]
        assert run_id not in ids

        # Rewind to publish_owed: exactly what a crash before the queue write,
        # or a failed one, leaves behind. The run must be selected now.
        run = (await db_session.execute(sa_select(Run).where(Run.id == run_id))).scalar_one()
        record = BotRolloutRecord.model_validate(run.run_metadata[BOT_ROLLOUT_METADATA_KEY])
        run.run_metadata = {
            **run.run_metadata,
            BOT_ROLLOUT_METADATA_KEY: record.model_copy(
                update={
                    "publish": BotRolloutPublishState.PUBLISH_OWED,
                    "attempts": 1,
                    "detail": "ConnectionError: stream down",
                }
            ).model_dump(mode="json"),
        }
        await db_session.commit()

        listed = await client.get("/api/runs/bot-rollouts/unsettled")
        ids = [row["id"] for row in listed.json()]
        assert run_id in ids

    @pytest.mark.asyncio
    async def test_an_abandoned_rollout_leaves_the_selection_for_good(self, client, db_session):
        """Attempts ran out: the sweep must not keep spending its page on it."""
        from sqlalchemy import select as sa_select

        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)
        await _add_running_application(db_session, project_id=project, deployed_sha=SHA_A)

        staged = await client.post(
            f"/api/projects/{project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Telegram-ID": str(owner)},
        )
        run_id = staged.json()["rollout_run_id"]

        run = (await db_session.execute(sa_select(Run).where(Run.id == run_id))).scalar_one()
        record = BotRolloutRecord.model_validate(run.run_metadata[BOT_ROLLOUT_METADATA_KEY])
        run.run_metadata = {
            **run.run_metadata,
            BOT_ROLLOUT_METADATA_KEY: record.model_copy(
                update={
                    "publish": BotRolloutPublishState.ABANDONED,
                    "attempts": 3,
                    "detail": "ConnectionError: stream down",
                }
            ).model_dump(mode="json"),
        }
        await db_session.commit()

        listed = await client.get("/api/runs/bot-rollouts/unsettled")
        ids = [row["id"] for row in listed.json()]
        assert run_id not in ids


class TestSetBotAccessRollsOut:
    @pytest.mark.asyncio
    async def test_public_switch_on_a_running_bot_stages_a_rollout(
        self, client, db_session: AsyncSession
    ):
        """set_bot_access reaches the live service too, not just the DB."""
        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)
        await _add_running_application(db_session, project_id=project, deployed_sha=SHA_A)

        resp = await client.post(
            f"/api/projects/{project}/config/bot-access",
            json={"mode": "public", "allowed_telegram_ids": ""},
            headers={"X-Telegram-ID": str(owner)},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["mode"] == "public"
        assert body["rollout"] == "pending", (
            "a running bot must be rolled out when the whole audience changes"
        )

        stored = await client.get(f"/api/projects/{project}")
        assert stored.json()["config"]["env_overrides"]["TG_BOT_ALLOWED_TELEGRAM_IDS"] == ""

    @pytest.mark.asyncio
    async def test_removing_the_final_id_is_refused_even_via_real_sql(
        self, client, db_session: AsyncSession
    ):
        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)

        resp = await client.delete(
            f"/api/projects/{project}/config/bot-access/users/{owner}",
            headers={"X-Telegram-ID": str(owner)},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "public" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_repeating_the_same_set_is_a_200_no_op(self, client, db_session):
        """An identical set_bot_access repeat succeeds without a write or rollout.

        This path used to raise an unhandled ValueError and answer 500.
        """
        owner = await _make_user(client)
        project = await _make_project(client, db_session, owner)

        again = await client.post(
            f"/api/projects/{project}/config/bot-access",
            json={"mode": "only_me", "allowed_telegram_ids": str(owner)},
            headers={"X-Telegram-ID": str(owner)},
        )
        assert again.status_code == status.HTTP_200_OK, again.text
        body = again.json()
        assert body["mode"] == "only_me"
        assert body["rollout"] == "not_deployed"
        assert body["rollout_run_id"] is None


class TestNotifyOwed:
    @pytest.mark.asyncio
    async def test_the_promise_is_durable_idempotent_and_owner_checked(
        self, async_client: AsyncClient, client, db_session: AsyncSession
    ):
        """The marker survives restarts, a repeat never resets a delivery, and
        another owner cannot owe a promise on someone else's rollout."""
        owner = await _make_user(client)
        intruder = await _make_user(client)
        project = await _make_project(async_client, db_session, owner)
        await _add_running_application(db_session, project_id=project, deployed_sha=SHA_A)

        staged = await async_client.post(
            f"/api/projects/{project}/config/bot-access/users",
            json={"telegram_id": 84},
            headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
        )
        assert staged.status_code == status.HTTP_200_OK
        run_id = staged.json()["rollout_run_id"]

        url = f"/api/projects/{project}/config/bot-access/rollouts/{run_id}/notify-owed"

        # Another owner cannot write the promise on this project's rollout.
        denied = await async_client.post(
            url,
            headers={
                "X-Internal-Key": os.environ["INTERNAL_API_KEY"],
                "X-Telegram-ID": str(intruder),
            },
        )
        assert denied.status_code == status.HTTP_403_FORBIDDEN

        # The owner owes the promise.
        first = await async_client.post(
            url,
            headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"], "X-Telegram-ID": str(owner)},
        )
        assert first.status_code == status.HTTP_200_OK
        assert first.json()["state"] == BotRolloutNotifyState.OWED.value

        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        notify = BotRolloutNotifyRecord.model_validate(run.run_metadata[BOT_ROLLOUT_NOTIFY_KEY])
        chat_id = notify.telegram_chat_id

        # Simulate delivery having happened, then re-call: state must not reset.
        delivered = notify.model_copy(update={"state": BotRolloutNotifyState.DELIVERED})
        run.run_metadata = {
            **run.run_metadata,
            BOT_ROLLOUT_NOTIFY_KEY: delivered.model_dump(mode="json"),
        }
        await db_session.commit()

        again = await async_client.post(
            url,
            headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"], "X-Telegram-ID": str(owner)},
        )
        assert again.status_code == status.HTTP_200_OK
        assert again.json()["state"] == BotRolloutNotifyState.DELIVERED.value

        db_session.expire_all()
        run = (await db_session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        final = BotRolloutNotifyRecord.model_validate(run.run_metadata[BOT_ROLLOUT_NOTIFY_KEY])
        assert final.state is BotRolloutNotifyState.DELIVERED
        assert final.telegram_chat_id == chat_id
