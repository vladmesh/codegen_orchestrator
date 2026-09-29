"""CI-only PostgreSQL/Redis proof for fenced initial-owner deployment recovery.

The merged PR/image observations are controlled fixtures. No healthy deployment,
worker, live GitHub, platform credential or production mutation is exercised.
"""

import asyncio
from datetime import UTC, datetime
import json
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.models import Run, Story, UsersGrantIntent
from shared.queues import DEPLOY_QUEUE
from src.dependencies import create_lk_jwt, get_redis_client
from src.main import app
from src.routers.projects import access

HEAD = "a" * 40
BUILT = "e" * 40
CANARY = "123456789:AA-initial-owner-secret-canary"


@pytest.fixture
async def initial_epoch(async_client):
    suffix = uuid.uuid4().hex[:10]
    owner = (
        await async_client.post(
            "/api/users/",
            json={
                "telegram_id": uuid.uuid4().int % 1_000_000_000,
                "username": f"owner_{suffix}",
            },
        )
    ).json()
    project = (
        await async_client.post(
            "/api/projects/",
            json={
                "title": "Initial retry",
                "initiating_run_id": f"initial-{suffix}",
                "config": {"modules": ["backend", "tg_bot"]},
            },
            headers={"X-Telegram-ID": str(owner["telegram_id"])},
        )
    ).json()
    project_id = project["id"]
    await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": suffix,
            "git_url": f"https://example.test/{suffix}.git",
            "role": "primary",
        },
    )
    story = (
        await async_client.post(
            "/api/stories/",
            json={
                "project_id": project_id,
                "title": "First deployment",
            },
        )
    ).json()
    story_id = story["id"]
    for action in ("start", "pr_review"):
        response = await async_client.post(f"/api/stories/{story_id}/{action}")
        assert response.status_code == 200, response.text
    response = await async_client.patch(
        f"/api/stories/{story_id}",
        json={
            "pr_number": 42,
            "generated_product_timeline": {
                "pull_request": {
                    "number": 42,
                    "state": "closed",
                    "head_sha": HEAD,
                    "merge_commit_sha": BUILT,
                    "merged_at": datetime.now(UTC).isoformat(),
                }
            },
        },
    )
    assert response.status_code == 200, response.text
    await _ceiling(async_client, 2)
    body = {
        "kind": "initial_owner",
        "story_id": story_id,
        "head_sha": HEAD,
        "deployed_commit_sha": BUILT,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {create_lk_jwt(owner['id'])}"},
    ) as human:
        yield {
            "project": project_id,
            "owner": owner,
            "story": story_id,
            "body": body,
            "lifecycle": f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            "human": human,
        }
    await _ceiling(async_client, 3)


async def _ceiling(client, value):
    response = await client.post(
        "/api/system-configs/",
        json={
            "key": "deploy.max_deploy_retries",
            "value": value,
            "category": "deploy",
        },
    )
    assert response.status_code == 201, response.text


async def _call(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _fail_run(client, run_id):
    response = await client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "failed",
            "error_message": f"controlled failure {CANARY}",
        },
    )
    assert response.status_code == 200, response.text


async def _exhaust(client, epoch):
    runs = []
    for _ in range(2):
        result = await _call(client, epoch["lifecycle"], epoch["body"])
        assert result["disposition"] == "dispatched"
        runs.append(result["execution_run_id"])
        if len(runs) == 1:
            response = await client.post(f"/api/stories/{epoch['story']}/deploy")
            assert response.status_code == 200, response.text
        await _fail_run(client, runs[-1])
    exhausted = await _call(client, epoch["lifecycle"], epoch["body"])
    assert exhausted["disposition"] == "exhausted"
    assert exhausted["exhaustion"]["exhausted_execution_run_id"] == runs[-1]
    return exhausted, runs


async def _intent(client, epoch, intent_id):
    response = await client.get(f"/api/projects/{epoch['project']}/users/grant-intents/{intent_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _owner_notice(client, story_id):
    response = await client.get(f"/api/stories/{story_id}/owner-notification")
    assert response.status_code == 200, response.text
    return response.json()


async def _messages(redis, project):
    rows = await redis.xrange(DEPLOY_QUEUE)
    messages = [json.loads(fields[b"data"]) for _, fields in rows]
    return [message for message in messages if message["project_id"] == project]


@pytest.mark.asyncio
async def test_same_intent_bounded_epochs_and_old_command_after_fast_exhaustion(
    async_client,
    initial_epoch,
    redis_client,
):
    e = initial_epoch
    exhausted, old_runs = await _exhaust(async_client, e)
    path = f"/api/projects/{e['project']}/users/grant-intents/{exhausted['intent_id']}/retry"
    command = exhausted["exhaustion"]["retry_command"]
    before = await _intent(e["human"], e, exhausted["intent_id"])
    completion = await async_client.post(
        f"/api/projects/{e['project']}/users/grant-intents/{exhausted['intent_id']}/complete",
        json={"execution_run_id": old_runs[-1], "active": False},
    )
    assert completion.status_code == 200 and completion.json()["status"] == "failed"
    assert await _intent(e["human"], e, exhausted["intent_id"]) == before
    historical = [(await async_client.get(f"/api/runs/{r}")).json() for r in old_runs]
    stopped = (await async_client.get(f"/api/stories/{e['story']}")).json()
    assert stopped["status"] == "failed"
    assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
    assert "retry_initial_owner_deployment" in stopped["quarantine_reason"]["detail"]
    notice = await _owner_notice(async_client, e["story"])
    assert notice["state"] == notice["admin_state"] == "owed"
    assert "retry_initial_owner_deployment" in notice["text"]
    assert CANARY not in json.dumps(exhausted) + json.dumps(notice)
    repeated = await _call(async_client, e["lifecycle"], e["body"])
    assert repeated == exhausted
    assert await _owner_notice(async_client, e["story"]) == notice
    await _ceiling(async_client, 3)
    assert (await _call(async_client, e["lifecycle"], e["body"]))["disposition"] == "exhausted"
    await _ceiling(async_client, 2)

    results = await asyncio.gather(*[_call(e["human"], path, command) for _ in range(4)])
    admitted = [r for r in results if r["disposition"] == "dispatched"]
    assert len(admitted) == 1
    assert all(r["disposition"] in {"dispatched", "in_flight"} for r in results)
    fresh = admitted[0]["execution_run_id"]
    after = await _intent(e["human"], e, exhausted["intent_id"])
    assert after["id"] == before["id"] and after["target_sha"] == before["target_sha"]
    assert after["target_history"] == before["target_history"]
    assert after["attempts"] == 1 and len(after["retry_history"]) == 1
    history = after["retry_history"][0]
    assert history["actor"] == f"user:{e['owner']['id']}"
    assert history["attempts"] == 2 and history["sha"] == HEAD
    assert history["expected_execution_run_id"] == old_runs[-1]
    assert history["story_stop"] == stopped["quarantine_reason"]
    run = (await async_client.get(f"/api/runs/{fresh}")).json()
    assert run["story_id"] == e["story"] and run["user_id"] == e["owner"]["id"]
    assert run["run_metadata"]["head_sha"] == HEAD
    assert run["run_metadata"]["deployed_commit_sha"] == BUILT
    recovered = (await async_client.get(f"/api/stories/{e['story']}")).json()
    assert recovered["status"] == "deploying" and recovered["quarantine_reason"] is None
    assert recovered["reopened_at"] == stopped["reopened_at"]
    assert len(await _messages(redis_client, e["project"])) == 3
    for r, original in zip(old_runs, historical, strict=True):
        assert (await async_client.get(f"/api/runs/{r}")).json() == original

    await _fail_run(async_client, fresh)
    next_run = await _call(async_client, e["lifecycle"], e["body"])
    await _fail_run(async_client, next_run["execution_run_id"])
    final = await _call(async_client, e["lifecycle"], e["body"])
    assert final["disposition"] == "exhausted"
    replay = await _call(e["human"], path, command)
    assert replay["disposition"] == "stale_target"
    current = await _intent(e["human"], e, exhausted["intent_id"])
    assert current["attempts"] == 2 and len(current["retry_history"]) == 1
    assert len(await _messages(redis_client, e["project"])) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["publication", "lost_response", "rollback"])
async def test_retry_interruptions_keep_one_real_epoch(
    async_client,
    initial_epoch,
    redis_client,
    monkeypatch,
    mode,
):
    e = initial_epoch
    exhausted, old_runs = await _exhaust(async_client, e)
    path = f"/api/projects/{e['project']}/users/grant-intents/{exhausted['intent_id']}/retry"
    command = exhausted["exhaustion"]["retry_command"]
    before = await _intent(async_client, e, exhausted["intent_id"])
    story_before = (await async_client.get(f"/api/stories/{e['story']}")).json()
    notice_before = await _owner_notice(async_client, e["story"])
    if mode == "publication":
        stream = get_redis_client()
        publish = stream.publish_message

        async def interrupted(*args, **kwargs):
            raise ConnectionError("controlled Redis interruption")

        monkeypatch.setattr(stream, "publish_message", interrupted)
        response = await e["human"].post(path, json=command)
        assert response.status_code == 503
        owed = await _intent(async_client, e, exhausted["intent_id"])
        assert owed["status"] == "publish_owed" and owed["attempts"] == 1
        monkeypatch.setattr(stream, "publish_message", publish)
        # Normal automatic recovery may publish the admitted Run, but cannot
        # acquire another reset or claim it created that execution.
        recovery = await _call(async_client, e["lifecycle"], e["body"])
        assert recovery["disposition"] == "in_flight" and recovery["execution_run_id"] is None
        final = await _intent(async_client, e, exhausted["intent_id"])
        assert final["execution_run_id"] == owed["execution_run_id"]
    else:
        dispatch = access._dispatch_lifecycle

        async def interrupted(*args, **kwargs):
            if mode == "lost_response":
                await dispatch(*args, **kwargs)
                raise ConnectionError("controlled committed response loss")
            raise asyncio.CancelledError()

        monkeypatch.setattr(access, "_dispatch_lifecycle", interrupted)
        # BaseHTTPMiddleware observes a cancelled handler as an ended stream;
        # the actual ASGI transport raises this exact error before a response.
        with pytest.raises(
            ConnectionError if mode == "lost_response" else RuntimeError,
            match=(
                "controlled committed response loss"
                if mode == "lost_response"
                else r"^No response returned\.$"
            ),
        ):
            await e["human"].post(path, json=command)
        monkeypatch.setattr(access, "_dispatch_lifecycle", dispatch)
        if mode == "rollback":
            assert await _intent(async_client, e, exhausted["intent_id"]) == before
            assert (await async_client.get(f"/api/stories/{e['story']}")).json() == story_before
            assert await _owner_notice(async_client, e["story"]) == notice_before
            assert len(await _messages(redis_client, e["project"])) == 2
        replay = await _call(e["human"], path, command)
        assert replay["disposition"] == ("dispatched" if mode == "rollback" else "in_flight")
        final = await _intent(async_client, e, exhausted["intent_id"])
    assert len(final["retry_history"]) == 1 and final["attempts"] == 1
    assert final["execution_run_id"] not in old_runs
    assert len(await _messages(redis_client, e["project"])) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack",
    [
        "bare_header",
        "unauthenticated",
        "service",
        "foreign_owner",
        "forged_actor",
        "automatic_flag",
    ],
)
async def test_only_authenticated_authorized_human_can_reset(
    async_client,
    initial_epoch,
    redis_client,
    attack,
):
    e = initial_epoch
    exhausted, _ = await _exhaust(async_client, e)
    before = await _intent(async_client, e, exhausted["intent_id"])
    command = exhausted["exhaustion"]["retry_command"]
    path = f"/api/projects/{e['project']}/users/grant-intents/{exhausted['intent_id']}/retry"
    if attack == "service":
        response = await async_client.post(path, json=command)
        assert response.status_code == 403
    elif attack in {"forged_actor", "automatic_flag"}:
        command = command | (
            {"actor": f"user:{e['owner']['id']}"}
            if attack == "forged_actor"
            else {"explicit_user_retry": True}
        )
        response = await (e["human"] if attack == "forged_actor" else async_client).post(
            path if attack == "forged_actor" else e["lifecycle"],
            json=command if attack == "forged_actor" else e["body"] | command,
        )
        assert response.status_code == 422
    else:
        headers = {}
        if attack == "bare_header":
            headers["X-Telegram-ID"] = str(e["owner"]["telegram_id"])
        elif attack == "foreign_owner":
            user = (
                await async_client.post(
                    "/api/users/", json={"telegram_id": uuid.uuid4().int % 1_000_000_000}
                )
            ).json()
            headers["Authorization"] = f"Bearer {create_lk_jwt(user['id'])}"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as attacker:
            response = await attacker.post(path, json=command)
        assert response.status_code == (403 if attack == "foreign_owner" else 401)
    assert await _intent(async_client, e, exhausted["intent_id"]) == before
    assert len(await _messages(redis_client, e["project"])) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        "cycle",
        "pr",
        "sha",
        "built",
        "unrelated_stop",
        "owner",
        "identity",
        "kind",
        "foreign_intent",
        "missing_run",
        "live",
        "archived",
    ],
)
async def test_current_native_evidence_fences_recovery(
    async_client,
    initial_epoch,
    db_session,
    redis_client,
    replacement,
):
    e = initial_epoch
    exhausted, runs = await _exhaust(async_client, e)
    intent_id = exhausted["intent_id"]
    story = await db_session.get(Story, e["story"])
    intent = await db_session.get(UsersGrantIntent, intent_id)
    source = await db_session.get(Run, runs[-1])
    if replacement == "cycle":
        story.reopened_at = datetime.now(UTC)
    elif replacement == "pr":
        story.pr_number = 43
    elif replacement in {"sha", "built"}:
        timeline = dict(story.generated_product_timeline)
        timeline["pull_request"] = timeline["pull_request"] | {
            "head_sha" if replacement == "sha" else "merge_commit_sha": "b" * 40,
        }
        story.generated_product_timeline = timeline
    elif replacement == "unrelated_stop":
        story.quarantine_reason = {"reason": "unrelated quarantine"}
    elif replacement == "owner":
        replacement_owner = (
            await async_client.post(
                "/api/users/", json={"telegram_id": uuid.uuid4().int % 1_000_000_000}
            )
        ).json()
        project = await access.load_locked_project(db_session, uuid.UUID(e["project"]))
        project.owner_id = replacement_owner["id"]
    elif replacement == "kind":
        intent.kind = "add_user"
    elif replacement == "identity":
        intent.external_id = "another-identity"
    elif replacement == "archived":
        story.status = "archived"
    elif replacement == "missing_run":
        source.run_metadata = {"head_sha": HEAD}
    elif replacement == "live":
        db_session.add(
            Run(
                id=f"unrelated-{uuid.uuid4().hex}",
                project_id=uuid.UUID(e["project"]),
                story_id=e["story"],
                type="deploy",
                status="queued",
            )
        )
    elif replacement == "foreign_intent":
        foreign = (
            await async_client.post(
                "/api/projects/",
                json={
                    "title": "Foreign resource",
                    "initiating_run_id": "foreign-resource",
                    "config": {"modules": ["tg_bot"]},
                },
                headers={"X-Telegram-ID": str(e["owner"]["telegram_id"])},
            )
        ).json()
        admitted = await _call(
            async_client,
            f"/api/projects/{foreign['id']}/users/grant-intents/lifecycle",
            {"kind": "initial_owner", "head_sha": "b" * 40, "deployed_commit_sha": "f" * 40},
        )
        intent_id = admitted["intent_id"]
    await db_session.commit()
    before = await _intent(async_client, e, exhausted["intent_id"])
    path = f"/api/projects/{e['project']}/users/grant-intents/{intent_id}/retry"
    response = await e["human"].post(path, json=exhausted["exhaustion"]["retry_command"])
    assert response.status_code in {403, 404, 409}, response.text
    assert await _intent(async_client, e, exhausted["intent_id"]) == before
    assert len(await _messages(redis_client, e["project"])) == 2


@pytest.mark.asyncio
async def test_released_bare_failure_and_real_admin_retry(async_client, initial_epoch, db_session):
    e = initial_epoch
    exhausted, runs = await _exhaust(async_client, e)
    story = await db_session.get(Story, e["story"])
    source = await db_session.get(Run, runs[-1])
    # Exact previously released shape: old immutable input, bare fail action,
    # no typed stop/cycle metadata. The real Run/intent/PR identity remains.
    source.run_metadata = {
        k: v
        for k, v in source.run_metadata.items()
        if k not in {"grant_story_cycle", "grant_pr_number", "grant_epoch", "grant_attempt"}
    }
    story.quarantine_reason = None
    story.owner_notification = None
    await db_session.commit()
    admin = (
        await async_client.post(
            "/api/users/",
            json={
                "telegram_id": uuid.uuid4().int % 1_000_000_000,
                "is_admin": True,
            },
        )
    ).json()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {create_lk_jwt(admin['id'])}"},
    ) as human:
        result = await _call(
            human,
            f"/api/projects/{e['project']}/users/grant-intents/{exhausted['intent_id']}/retry",
            exhausted["exhaustion"]["retry_command"],
        )
    assert result["disposition"] == "dispatched"
    intent = await _intent(async_client, e, exhausted["intent_id"])
    assert intent["retry_history"][0]["actor"] == f"user:{admin['id']}"
    assert (await async_client.get(f"/api/stories/{e['story']}")).json()["status"] == "deploying"


@pytest.mark.asyncio
async def test_zero_ceiling_remains_terminal_after_policy_fix(
    async_client, initial_epoch, redis_client
):
    e = initial_epoch
    await _ceiling(async_client, 0)
    zero = await _call(async_client, e["lifecycle"], e["body"])
    assert zero["exhaustion"]["action"] is None
    assert zero["exhaustion"]["retry_command"] is None
    path = f"/api/projects/{e['project']}/users/grant-intents/{zero['intent_id']}/retry"
    response = await _call(e["human"], path, {"expected_execution_run_id": "no-such-attempt"})
    assert response["disposition"] == "stale_target"
    assert await _messages(redis_client, e["project"]) == []
    await _ceiling(async_client, 2)
    fixed = await _call(async_client, e["lifecycle"], e["body"])
    assert fixed["disposition"] == "exhausted"
    assert await _messages(redis_client, e["project"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interference", ["none", "wrong_pr", "unrelated_stop", "live_run"])
async def test_zero_ceiling_stops_current_merged_story_without_a_run(
    async_client, initial_epoch, redis_client, db_session, interference
):
    e = initial_epoch
    story = (await async_client.get(f"/api/stories/{e['story']}")).json()
    repo = (
        await async_client.get("/api/repositories/", params={"project_id": e["project"]})
    ).json()[0]
    timeline = story["generated_product_timeline"] | {
        "latest_ci_observation": {
            "ci_run_id": 12345,
            "ci_status": "completed",
            "ci_conclusion": "success",
        },
        "ci_runs": [
            {
                "id": 12345,
                "branch": "main",
                "head_sha": BUILT,
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "deploy_observation": {
            "story_id": e["story"],
            "project_id": e["project"],
            "repository_url": repo["git_url"],
            "observed_at": datetime.now(UTC).isoformat(),
        },
    }
    response = await async_client.patch(
        f"/api/stories/{e['story']}", json={"generated_product_timeline": timeline}
    )
    assert response.status_code == 200, response.text
    if interference == "unrelated_stop":
        row = await db_session.get(Story, e["story"])
        row.quarantine_reason = {"reason": "unrelated quarantine"}
        await db_session.commit()
    elif interference == "live_run":
        db_session.add(
            Run(
                id=f"unrelated-{uuid.uuid4().hex}",
                project_id=uuid.UUID(e["project"]),
                story_id=e["story"],
                type="deploy",
                status="queued",
            )
        )
        await db_session.commit()
    await _ceiling(async_client, 0)
    body = e["body"] | {"merged_pr_number": 43 if interference == "wrong_pr" else 42}
    before = (await async_client.get(f"/api/stories/{e['story']}")).json()
    if interference == "wrong_pr":
        refused = await async_client.post(e["lifecycle"], json=body)
        assert refused.status_code == 409
        assert (await async_client.get(f"/api/stories/{e['story']}")).json() == before
        assert await _messages(redis_client, e["project"]) == []
        return
    exhausted = await _call(async_client, e["lifecycle"], body)
    assert exhausted["disposition"] == "exhausted"
    assert exhausted["exhaustion"]["attempts"] == 0
    assert exhausted["exhaustion"]["exhausted_execution_run_id"] is None
    assert exhausted["exhaustion"]["action"] is None
    assert exhausted["exhaustion"]["retry_command"] is None
    if interference != "none":
        assert (await async_client.get(f"/api/stories/{e['story']}")).json() == before
        assert await _messages(redis_client, e["project"]) == []
        return
    stopped = (await async_client.get(f"/api/stories/{e['story']}")).json()
    assert stopped["status"] == "failed"
    assert stopped["quarantine_reason"]["code"] == "initial_owner_deployment_exhausted"
    assert "same-target retry is unavailable" in stopped["quarantine_reason"]["detail"].lower()
    assert "retry_initial_owner_deployment" not in stopped["quarantine_reason"]["detail"]
    notice = await _owner_notice(async_client, e["story"])
    assert notice["state"] == notice["admin_state"] == "owed"
    assert "same-target retry is unavailable" in notice["text"].lower()
    assert "retry_initial_owner_deployment" not in notice["text"] + notice["admin_text"]
    assert "can deliberately retry" not in notice["text"]
    readback = await _intent(e["human"], e, exhausted["intent_id"])
    assert readback["exhaustion"] == exhausted["exhaustion"]
    assert await _messages(redis_client, e["project"]) == []
    repeated = await _call(async_client, e["lifecycle"], body)
    assert exhausted["created"] is True and repeated["created"] is False
    assert repeated | {"created": True} == exhausted
    assert await _owner_notice(async_client, e["story"]) == notice
    await _ceiling(async_client, 2)
    raised = await _call(async_client, e["lifecycle"], body)
    assert raised["disposition"] == "exhausted"
    assert raised["exhaustion"]["action"] is None
    assert raised["exhaustion"]["retry_command"] is None
    assert await _owner_notice(async_client, e["story"]) == notice
    assert await _messages(redis_client, e["project"]) == []


@pytest.mark.asyncio
async def test_premature_human_retry_refuses_without_reset(
    async_client, initial_epoch, redis_client
):
    e = initial_epoch
    admitted = await _call(async_client, e["lifecycle"], e["body"])
    source = admitted["execution_run_id"]
    await _fail_run(async_client, source)
    response = await async_client.post(
        f"/api/projects/{e['project']}/users/grant-intents/{admitted['intent_id']}/complete",
        json={"execution_run_id": source, "active": False},
    )
    assert response.status_code == 200, response.text
    before = await _intent(async_client, e, admitted["intent_id"])
    assert before["status"] == "retryable" and before["attempts"] == 1
    path = f"/api/projects/{e['project']}/users/grant-intents/{admitted['intent_id']}/retry"
    refused = await e["human"].post(path, json={"expected_execution_run_id": source})
    assert refused.status_code == 409
    assert "not exhausted" in refused.json()["detail"]
    assert await _intent(async_client, e, admitted["intent_id"]) == before
    assert len(await _messages(redis_client, e["project"])) == 1


@pytest.mark.asyncio
async def test_applied_and_live_win(async_client, initial_epoch, redis_client):
    e = initial_epoch
    admitted = await _call(async_client, e["lifecycle"], e["body"])
    assert admitted["disposition"] == "dispatched"
    path = f"/api/projects/{e['project']}/users/grant-intents/{admitted['intent_id']}/retry"
    command = {"expected_execution_run_id": admitted["execution_run_id"]}
    live = await _call(e["human"], path, command)
    assert live["disposition"] == "in_flight" and live["execution_run_id"] is None
    complete = await async_client.post(
        f"/api/projects/{e['project']}/users/grant-intents/{admitted['intent_id']}/complete",
        json={"execution_run_id": admitted["execution_run_id"], "active": True},
    )
    assert complete.status_code == 200
    applied = await _call(e["human"], path, command)
    assert applied["disposition"] == "already_applied"
    assert len(await _messages(redis_client, e["project"])) == 1
