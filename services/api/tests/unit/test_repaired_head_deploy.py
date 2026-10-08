"""An administrator approves deploying the repaired head of an `images_not_published` park."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from shared.contracts.dto.repaired_head_deploy import (
    REPAIRED_HEAD_APPROVAL_KEY,
    RepairedHeadDeployApproval,
    RepairedHeadDeployCommand,
    approval_for_merge,
)
from shared.models import Story, WorkAdmissionAudit
from src.repaired_head_deploy import deploy_repaired_head

STOP_ID = "stop-images"
PR_HEAD = "a" * 40
MERGE = "b" * 40
REPAIRED = "c" * 40


class FakeGitHub:
    """The GitHub reads the approval makes, answering as a repaired repository does."""

    def __init__(self):
        self.pull_request = {
            "number": 5,
            "merged_at": "2026-10-08T10:00:00Z",
            "merge_commit_sha": MERGE,
            "head": {"sha": PR_HEAD},
        }
        self.default_branch = "main"
        self.on_default = True
        self.comparison = "ahead"
        self.calls = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get_pull_request(self, owner, repo, number):
        self.calls.append(("pull_request", owner, repo, number))
        return self.pull_request

    async def get_repo(self, owner, repo):
        self.calls.append(("repo", owner, repo))
        return SimpleNamespace(default_branch=self.default_branch)

    async def branch_contains_commit(self, owner, repo, branch, sha):
        self.calls.append(("contains", branch, sha))
        return self.on_default

    async def compare_commits_status(self, owner, repo, base_sha, head_sha):
        self.calls.append(("compare", base_sha, head_sha))
        return self.comparison


class World(SimpleNamespace):
    """A story parked because its merge commit's images never appeared."""


@pytest.fixture
def world(monkeypatch):
    now = datetime.now(UTC)
    pid = uuid.uuid4()
    cause = {
        "deploy_outcome": "images_not_published",
        "head_sha": PR_HEAD,
        "deployed_commit_sha": MERGE,
        "ci_run_id": 9100,
        "ci_conclusion": "failure",
        "detail": "ci.yml run 9100 for the commit concluded failure",
    }
    story = Story(
        id="story-parked",
        project_id=pid,
        title="Recipe box",
        type="product",
        status="waiting_human_review",
        waiting_on="human_review",
        priority=0,
        created_by="po",
        unverified_decisions=[],
        created_at=now - timedelta(days=1),
        updated_at=now,
        pr_number=5,
        quarantine_reason=cause,
        engineering_stop={"id": STOP_ID, "actor": "scheduler", "stopped_at": now.isoformat()},
        generated_product_timeline={"pull_request": {"number": 5, "merge_commit_sha": MERGE}},
    )
    repository = SimpleNamespace(git_url="https://github.com/fictional-org/recipe-box")
    github = FakeGitHub()
    db = SimpleNamespace(
        # The live deploy Run lookup, then the primary repository.
        scalar=AsyncMock(side_effect=[None, repository]),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.routers._story_helpers._get_story_for_update", AsyncMock(return_value=story)
    )
    monkeypatch.setattr("src.repaired_head_deploy.GitHubAppClient", github)
    return World(now=now, story=story, cause=cause, github=github, db=db, repository=repository)


def command(**overrides):
    fields = {"stop_id": STOP_ID, "deployed_commit_sha": REPAIRED}
    return RepairedHeadDeployCommand(**{**fields, **overrides})


async def approve(world, **overrides):
    return await deploy_repaired_head(world.story.id, command(**overrides), "user:7", world.db)


@pytest.mark.asyncio
async def test_approval_releases_the_stop_records_it_and_returns_the_story_to_the_poller(world):
    story = await approve(world)

    assert story is world.story
    assert story.status == "pr_review" and story.waiting_on == "ci"
    assert story.quarantine_reason is None
    assert story.engineering_stop["released_at"] is not None
    assert story.engineering_stop["release_actor"] == "user:7"
    audits = [c.args[0] for c in world.db.add.call_args_list]
    assert [(a.subject, a.outcome) for a in audits if isinstance(a, WorkAdmissionAudit)] == [
        ("engineering_stop", "released")
    ]
    timeline = story.generated_product_timeline
    assert timeline["pull_request"] == {"number": 5, "merge_commit_sha": MERGE}
    approval = RepairedHeadDeployApproval.model_validate(timeline[REPAIRED_HEAD_APPROVAL_KEY])
    assert approval.model_dump(exclude={"approved_at"}) == {
        "actor": "user:7",
        "pr_number": 5,
        "head_sha": PR_HEAD,
        "merge_commit_sha": MERGE,
        "approved_commit_sha": REPAIRED,
        "superseded_commit_sha": MERGE,
        "quarantine_reason": world.cause,
    }
    assert approval.approved_at >= world.now
    # The poller reads it back for exactly this merge.
    assert approval_for_merge(timeline, pr_number=5, merge_commit_sha=MERGE) == approval
    assert approval_for_merge(timeline, pr_number=6, merge_commit_sha=MERGE) is None
    assert ("contains", "main", REPAIRED) in world.github.calls
    assert ("compare", MERGE, REPAIRED) in world.github.calls
    world.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_second_refusal_is_approved_against_the_commit_it_named(world):
    """After the approved head's images also failed, the next repair supersedes it."""
    newer = "d" * 40
    world.cause["deployed_commit_sha"] = REPAIRED

    story = await approve(world, deployed_commit_sha=newer)

    approval = story.generated_product_timeline[REPAIRED_HEAD_APPROVAL_KEY]
    assert approval["superseded_commit_sha"] == REPAIRED
    assert approval["approved_commit_sha"] == newer
    assert approval["merge_commit_sha"] == MERGE
    assert ("compare", REPAIRED, newer) in world.github.calls


@pytest.mark.asyncio
async def test_a_repeated_approval_is_refused_without_a_second_effect(world):
    await approve(world)
    world.db.add.reset_mock()
    world.db.scalar = AsyncMock(side_effect=[None, world.repository])

    with pytest.raises(HTTPException) as refused:
        await approve(world)

    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "story_not_waiting_human_review"
    world.db.add.assert_not_called()
    world.db.commit.assert_awaited_once()


def _story_deploying(world):
    world.story.status = "deploying"


def _qa_park(world):
    world.story.quarantine_reason = {"blocker": {"category": "server_unavailable"}}


def _no_cause(world):
    world.story.quarantine_reason = None


def _other_stop_id(world):
    return {"stop_id": "stop-other"}


def _released_stop(world):
    world.story.engineering_stop["released_at"] = world.now.isoformat()


def _no_stop(world):
    world.story.engineering_stop = None


def _no_pull_request(world):
    world.story.pr_number = None


def _live_deploy(world):
    world.db.scalar = AsyncMock(side_effect=["deploy-poll-live", world.repository])


def _refusal_without_commit(world):
    world.cause.pop("deployed_commit_sha")


def _same_commit(world):
    return {"deployed_commit_sha": MERGE}


def _no_repository(world):
    world.db.scalar = AsyncMock(side_effect=[None, None])


def _foreign_repository(world):
    world.repository.git_url = "https://gitlab.example/fictional-org/recipe-box"


def _unmerged_pull_request(world):
    world.github.pull_request["merged_at"] = None


def _changed_pull_request(world):
    world.github.pull_request["head"] = {"sha": "e" * 40}


def _off_default_branch(world):
    world.github.on_default = False


def _diverged(world):
    world.github.comparison = "diverged"


def _behind(world):
    world.github.comparison = "behind"


def _identical(world):
    world.github.comparison = "identical"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        (_story_deploying, "story_not_waiting_human_review"),
        (_qa_park, "not_images_not_published"),
        (_no_cause, "not_images_not_published"),
        (_other_stop_id, "engineering_stop_mismatch"),
        (_released_stop, "engineering_stop_mismatch"),
        (_no_stop, "engineering_stop_mismatch"),
        (_no_pull_request, "story_has_no_pull_request"),
        (_live_deploy, "deploy_run_live"),
        (_refusal_without_commit, "refused_commit_unknown"),
        (_same_commit, "commit_not_ahead"),
        (_no_repository, "repository_missing"),
        (_foreign_repository, "repository_unowned"),
        (_unmerged_pull_request, "pull_request_not_merged"),
        (_changed_pull_request, "pull_request_changed"),
        (_off_default_branch, "commit_not_on_default_branch"),
        (_diverged, "commit_not_ahead"),
        (_behind, "commit_not_ahead"),
        (_identical, "commit_not_ahead"),
    ],
)
async def test_every_unmet_precondition_is_a_distinct_409_that_changes_nothing(
    world, arrange, code
):
    overrides = arrange(world) or {}
    before = {
        column: (value.copy() if isinstance(value, dict) else value)
        for column in ("status", "waiting_on", "quarantine_reason", "engineering_stop")
        for value in [getattr(world.story, column)]
    }
    timeline_before = dict(world.story.generated_product_timeline)

    with pytest.raises(HTTPException) as refused:
        await approve(world, **overrides)

    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == code
    assert {column: getattr(world.story, column) for column in before} == before
    assert world.story.generated_product_timeline == timeline_before
    world.db.add.assert_not_called()
    world.db.commit.assert_not_awaited()


@pytest.mark.parametrize("sha", ["C" * 40, "c" * 39, "c" * 64, ""])
def test_the_request_names_one_full_lowercase_commit(sha):
    with pytest.raises(ValidationError):
        command(deployed_commit_sha=sha)


@pytest.mark.asyncio
@pytest.mark.slow(reason="the first ASGI request through the app builds its middleware stack")
async def test_the_bearer_admin_route_approves_as_that_administrator(world):
    from httpx import ASGITransport, AsyncClient
    from internal_caller import INTERNAL_HEADERS

    from src.database import get_async_session
    from src.dependencies import require_bearer_admin
    from src.main import app

    async def session():
        yield world.db

    app.dependency_overrides[get_async_session] = session
    app.dependency_overrides[require_bearer_admin] = lambda: SimpleNamespace(id=7)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            response = await client.post(
                f"/api/stories/{world.story.id}/deploy-repaired-head",
                json=command().model_dump(mode="json"),
            )
            malformed = await client.post(
                f"/api/stories/{world.story.id}/deploy-repaired-head",
                json={"stop_id": STOP_ID, "deployed_commit_sha": "sha-6373dbc"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pr_review"
    assert body["generated_product_timeline"][REPAIRED_HEAD_APPROVAL_KEY]["actor"] == "user:7"
    assert malformed.status_code == 422


@pytest.mark.asyncio
@pytest.mark.slow(reason="the first ASGI request through the app builds its middleware stack")
async def test_the_route_requires_a_bearer_administrator(world):
    from httpx import ASGITransport, AsyncClient
    from internal_caller import INTERNAL_HEADERS

    from src.database import get_async_session
    from src.main import app

    async def session():
        yield world.db

    app.dependency_overrides[get_async_session] = session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            response = await client.post(
                f"/api/stories/{world.story.id}/deploy-repaired-head",
                json=command().model_dump(mode="json"),
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
    assert world.story.status == "waiting_human_review"
    world.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [SimpleNamespace(id=3, is_admin=False), None])
async def test_only_the_internal_producer_may_write_an_approval_into_the_timeline(
    world, monkeypatch, actor
):
    from shared.contracts.dto.story import StoryUpdate
    from src.routers import stories

    monkeypatch.setattr(stories, "resolve_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(stories, "_get_story_for_update", AsyncMock(return_value=world.story))
    forged = {"pull_request": {}, REPAIRED_HEAD_APPROVAL_KEY: {"approved_commit_sha": "f" * 40}}
    body = StoryUpdate(generated_product_timeline=forged)

    if actor is not None:
        with pytest.raises(HTTPException) as refused:
            await stories.update_story(world.story.id, body, world.db, False, 3, None)
        assert refused.value.status_code == 403
        assert REPAIRED_HEAD_APPROVAL_KEY not in world.story.generated_product_timeline
        world.db.commit.assert_not_awaited()
    else:
        await stories.update_story(world.story.id, body, world.db, True, None, None)
        assert world.story.generated_product_timeline == forged
