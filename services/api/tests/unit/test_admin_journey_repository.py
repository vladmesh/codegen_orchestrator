"""The repository and diff read of a story, against a fake GitHub client and a fake session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
import httpx
from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.models import Repository, Story
from src import admin_journey_repository
from src.admin_journey_repository import read_story_repository
from src.database import get_async_session
from src.main import app
from src.routers.applications import _resolve_admin_deploy_head_sha
from src.schemas.admin_console import RepositoryHeadSource, RepositoryUnavailableReason

BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40
GIT_URL = "https://github.com/acme/shop.git"
TREE = ["README.md", "src/app.py"]


class FakeSession:
    def __init__(self, story=None, repository=None):
        self.story = story
        self.repository = repository

    async def get(self, model, key):
        assert model is Story
        return self.story

    async def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is Repository
        return self.repository


class FakeGitHub:
    """Answers like GitHub for one repository and records what it was asked."""

    def __init__(self, *, pr=None, branch_sha=None, files=None, error=None, repo_error=None):
        self.pr = pr
        self.branch_sha = branch_sha
        self.files = files if files is not None else [_file("src/app.py")]
        self.error = error
        self.repo_error = repo_error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def _answer(self, call, value):
        self.calls.append(call)
        if self.error is not None:
            if callable(self.error):
                await self.error()
            raise self.error
        return value

    async def get_pull_request(self, owner, repo, pr_number):
        return await self._answer(("pull", owner, repo, pr_number), self.pr)

    async def get_ref_sha(self, owner, repo, ref):
        return await self._answer(("ref", owner, repo, ref), self.branch_sha)

    async def get_repo(self, owner, repo):
        if self.repo_error is not None:
            self.calls.append(("repo", owner, repo))
            raise self.repo_error
        return await self._answer(("repo", owner, repo), SimpleNamespace(default_branch="main"))

    async def list_repo_files_recursive(self, owner, repo, ref):
        return await self._answer(("tree", owner, repo, ref), TREE)

    async def compare_commits(self, owner, repo, base, head):
        return await self._answer(("compare", owner, repo, base, head), self.files)


def _file(path, patch="@@ -1 +1 @@\n-a\n+b", status="modified"):
    item = {"filename": path, "status": status, "additions": 1, "deletions": 1}
    if patch is not None:
        item["patch"] = patch
    return item


def _pr(*, merged, changed_files=1):
    return {
        "number": 7,
        "state": "closed" if merged else "open",
        "merged": merged,
        "changed_files": changed_files,
        "base": {"ref": "main", "sha": BASE_SHA},
        "head": {"ref": "story/st-1", "sha": HEAD_SHA},
    }


def _session(*, pr_number=None, git_url=GIT_URL):
    return FakeSession(
        story=SimpleNamespace(id="st-1", project_id="p-1", pr_number=pr_number),
        repository=SimpleNamespace(git_url=git_url),
    )


async def _read(session, github):
    with patch("src.admin_journey_repository.GitHubAppClient", return_value=github):
        return await read_story_repository(session, "st-1")


@pytest.mark.parametrize("merged", [False, True], ids=["open-pr", "merged-pr"])
async def test_a_story_with_a_pull_request_compares_its_base_with_its_head(merged):
    github = FakeGitHub(pr=_pr(merged=merged))

    result = await _read(_session(pr_number=7), github)

    assert result.unavailable is None
    assert result.repository.model_dump() == {"owner": "acme", "name": "shop"}
    assert result.head.model_dump() == {
        "source": RepositoryHeadSource.PULL_REQUEST,
        "ref": "story/st-1",
        "sha": HEAD_SHA,
    }
    assert result.files == TREE
    assert [change.path for change in result.changes] == ["src/app.py"]
    assert (result.omitted_changes, result.omitted_changes_exact) == (0, True)
    assert ("compare", "acme", "shop", BASE_SHA, HEAD_SHA) in github.calls
    assert ("tree", "acme", "shop", HEAD_SHA) in github.calls


async def test_a_story_branch_without_a_pull_request_compares_the_default_branch_with_it():
    github = FakeGitHub(branch_sha=HEAD_SHA)

    result = await _read(_session(), github)

    assert result.unavailable is None
    assert result.head.model_dump() == {
        "source": RepositoryHeadSource.BRANCH,
        "ref": "story/st-1",
        "sha": HEAD_SHA,
    }
    assert result.files == TREE
    assert ("ref", "acme", "shop", "heads/story/st-1") in github.calls
    assert ("compare", "acme", "shop", "main", HEAD_SHA) in github.calls
    assert ("tree", "acme", "shop", HEAD_SHA) in github.calls


async def test_a_story_without_a_pull_request_or_branch_has_no_head():
    github = FakeGitHub(branch_sha=None)

    result = await _read(_session(), github)

    assert result.unavailable.reason is RepositoryUnavailableReason.NO_STORY_BRANCH
    assert (result.head, result.files, result.changes) == (None, [], [])
    assert result.repository.name == "shop"
    assert [call[0] for call in github.calls] == ["repo", "ref"]


async def test_a_repository_github_does_not_serve_is_unavailable_before_any_story_branch():
    github = FakeGitHub(branch_sha=None, repo_error=_status_error(404))

    result = await _read(_session(), github)

    assert result.unavailable.model_dump() == {
        "reason": RepositoryUnavailableReason.GITHUB_UNAVAILABLE,
        "detail": "404",
    }
    assert [call[0] for call in github.calls] == ["repo"]


async def test_a_pull_request_counts_omitted_changes_exactly_from_its_changed_files():
    listed = [_file(f"f{index}.py") for index in range(300)]
    github = FakeGitHub(pr=_pr(merged=True, changed_files=450), files=listed)

    result = await _read(_session(pr_number=7), github)

    assert len(result.changes) == admin_journey_repository.CHANGED_FILE_LIMIT
    assert result.omitted_changes == 450 - admin_journey_repository.CHANGED_FILE_LIMIT
    assert result.omitted_changes_exact is True


async def test_a_branch_comparison_at_the_github_file_cap_counts_a_lower_bound():
    cap = admin_journey_repository.GITHUB_COMPARE_FILE_CAP
    github = FakeGitHub(branch_sha=HEAD_SHA, files=[_file(f"f{index}.py") for index in range(cap)])

    result = await _read(_session(), github)

    assert result.omitted_changes == cap - admin_journey_repository.CHANGED_FILE_LIMIT
    assert result.omitted_changes_exact is False


async def test_changes_are_bounded_and_a_file_without_a_patch_has_a_null_patch(monkeypatch):
    monkeypatch.setattr(admin_journey_repository, "CHANGED_FILE_LIMIT", 3)
    monkeypatch.setattr(admin_journey_repository, "PATCH_LENGTH_LIMIT", 5)
    github = FakeGitHub(
        branch_sha=HEAD_SHA,
        files=[
            _file("long.py", patch="0123456789"),
            _file("short.py", patch="01234"),
            _file("logo.png", patch=None, status="added"),
            _file("dropped.py"),
            _file("dropped2.py"),
        ],
    )

    result = await _read(_session(), github)

    assert [change.model_dump(mode="json") for change in result.changes] == [
        {
            "path": "long.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "01234",
            "truncated": True,
        },
        {
            "path": "short.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "01234",
            "truncated": False,
        },
        {
            "path": "logo.png",
            "status": "added",
            "additions": 1,
            "deletions": 1,
            "patch": None,
            "truncated": False,
        },
    ]
    assert (result.omitted_changes, result.omitted_changes_exact) == (2, True)


async def test_a_project_without_a_primary_repository_reads_nothing_from_github():
    github = FakeGitHub(branch_sha=HEAD_SHA)
    session = FakeSession(story=SimpleNamespace(id="st-1", project_id="p-1", pr_number=None))

    result = await _read(session, github)

    assert result.unavailable.model_dump() == {
        "reason": RepositoryUnavailableReason.NO_REPOSITORY,
        "detail": None,
    }
    assert (result.repository, result.head, result.files, result.changes) == (None, None, [], [])
    assert github.calls == []


async def _never_answers():
    await asyncio.Event().wait()


def _status_error(code):
    request = httpx.Request("GET", "https://api.github.com/repos/acme/shop/pulls/7")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code))


@pytest.mark.parametrize(
    ("error", "timeout", "detail"),
    [
        (_status_error(404), None, "404"),
        (httpx.ConnectError("down"), None, "ConnectError"),
        (_never_answers, 0, "TimeoutError"),
    ],
    ids=["status", "transport", "timeout"],
)
async def test_a_github_failure_is_named_with_its_status_or_exception_class(
    monkeypatch, error, timeout, detail
):
    if timeout is not None:
        monkeypatch.setattr(admin_journey_repository, "GITHUB_READ_TIMEOUT_SECONDS", timeout)
    github = FakeGitHub(pr=_pr(merged=False), error=error)

    result = await _read(_session(pr_number=7), github)

    assert result.unavailable.model_dump() == {
        "reason": RepositoryUnavailableReason.GITHUB_UNAVAILABLE,
        "detail": detail,
    }
    assert result.repository.model_dump() == {"owner": "acme", "name": "shop"}
    assert (result.head, result.files, result.changes) == (None, [], [])


@pytest.fixture
def _session_override():
    def install(session):
        app.dependency_overrides[get_async_session] = lambda: session

    yield install
    app.dependency_overrides.pop(get_async_session, None)


async def test_the_repository_route_answers_admins_and_404s_an_unknown_story(_session_override):
    github = FakeGitHub(pr=_pr(merged=True))
    _session_override(_session(pr_number=7))

    with patch("src.admin_journey_repository.GitHubAppClient", return_value=github):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            anonymous = await http.get("/api/admin/v2/journeys/st-1/repository")
            found = await http.get(
                "/api/admin/v2/journeys/st-1/repository", headers=INTERNAL_HEADERS
            )
            _session_override(FakeSession())
            missing = await http.get(
                "/api/admin/v2/journeys/st-missing/repository", headers=INTERNAL_HEADERS
            )

    assert anonymous.status_code == 401
    assert found.status_code == 200, found.text
    assert found.json() == {
        "repository": {"owner": "acme", "name": "shop"},
        "head": {"source": "pull_request", "ref": "story/st-1", "sha": HEAD_SHA},
        "files": TREE,
        "changes": [
            {
                "path": "src/app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -1 +1 @@\n-a\n+b",
                "truncated": False,
            }
        ],
        "omitted_changes": 0,
        "omitted_changes_exact": True,
        "unavailable": None,
    }
    assert missing.status_code == 404


class _DeployHeadGitHub:
    asked = []

    async def get_default_branch_head_sha(self, owner, repo):
        self.asked.append((owner, repo))
        return HEAD_SHA


async def test_the_admin_deploy_reads_the_head_of_the_repository_its_git_url_names(monkeypatch):
    monkeypatch.setattr("src.routers.applications.GitHubAppClient", _DeployHeadGitHub)
    _DeployHeadGitHub.asked = []

    head = await _resolve_admin_deploy_head_sha(
        SimpleNamespace(git_url="git@github.com:acme/shop.git")
    )

    assert head == HEAD_SHA
    assert _DeployHeadGitHub.asked == [("acme", "shop")]
    with pytest.raises(HTTPException) as refused:
        await _resolve_admin_deploy_head_sha(
            SimpleNamespace(git_url="https://gitlab.com/acme/shop")
        )
    assert refused.value.status_code == 422
