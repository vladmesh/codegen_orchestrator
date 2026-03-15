from datetime import UTC, datetime, timedelta
import os
import time
from unittest.mock import patch

import httpx
import pytest
import respx

from shared.clients.github import GitHubAppClient, WorkflowNotFoundError


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ, {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY_PATH": "dummy.pem"}
    ):
        yield


@pytest.fixture
def client(mock_env):
    client = GitHubAppClient()
    client._private_key = "dummy_private_key"  # Bypass file loading
    return client


@pytest.fixture
def mock_jwt():
    with patch(
        "shared.clients.github.GitHubAppClient._generate_jwt", return_value="mock_jwt_token"
    ):
        yield


@pytest.mark.asyncio
async def test_get_installation_token_success(client, mock_jwt):
    installation_id = 999

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.post(f"/app/installations/{installation_id}/access_tokens").mock(
            return_value=httpx.Response(
                httpx.codes.CREATED,
                json={
                    "token": "ghs_new_token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
        )

        token = await client._get_installation_token(installation_id)
        assert token == "ghs_new_token"  # noqa: S105
        assert installation_id in client._token_cache
        assert client._token_cache[installation_id][0] == "ghs_new_token"  # noqa: S105


@pytest.mark.asyncio
async def test_get_installation_token_cached(client, mock_jwt):
    installation_id = 888
    # Pre-populate cache with a valid token
    expires_at = datetime.now(UTC) + timedelta(minutes=50)
    client._token_cache[installation_id] = ("cached_token", expires_at)

    async with respx.mock(base_url="https://api.github.com", assert_all_called=False) as respx_mock:
        # Should NOT make a request
        token_endpoint = respx_mock.post(f"/app/installations/{installation_id}/access_tokens")

        token = await client._get_installation_token(installation_id)

        assert token == "cached_token"  # noqa: S105
        assert not token_endpoint.called


@pytest.mark.asyncio
async def test_get_installation_token_expired(client, mock_jwt):
    installation_id = 777
    # Pre-populate cache with an expired token (less than 60s buffer)
    expires_at = datetime.now(UTC) + timedelta(seconds=30)
    client._token_cache[installation_id] = ("expired_token", expires_at)

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        respx_mock.post(f"/app/installations/{installation_id}/access_tokens").mock(
            return_value=httpx.Response(
                httpx.codes.CREATED,
                json={
                    "token": "new_refreshed_token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
        )

        token = await client._get_installation_token(installation_id)

        assert token == "new_refreshed_token"  # noqa: S105
        assert client._token_cache[installation_id][0] == "new_refreshed_token"  # noqa: S105


@pytest.mark.asyncio
async def test_rate_limiting_handling(client):
    # Mocking rate limit hit then success

    async with respx.mock(base_url="https://api.github.com") as respx_mock:
        # 1st request: 403 Rate Limit
        route = respx_mock.get("/rate_limit_test")
        route.side_effect = [
            httpx.Response(
                httpx.codes.FORBIDDEN,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 1),
                },
            ),
            httpx.Response(httpx.codes.OK, json={"ok": True}),
        ]

        # We need to mock asyncio.sleep to speed up test
        with patch("asyncio.sleep", return_value=None) as mock_sleep:
            resp = await client._make_request(
                "GET", "https://api.github.com/rate_limit_test", headers={}
            )

            assert resp.status_code == httpx.codes.OK
            assert resp.json() == {"ok": True}
            # Verify sleep was called
            assert mock_sleep.called


@pytest.mark.asyncio
async def test_get_file_contents_404(client, mock_jwt):
    owner, repo, path = "foo", "bar", "baz.txt"
    # Mock token retrieval
    client._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    with patch.object(client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/contents/{path}").mock(
                return_value=httpx.Response(httpx.codes.NOT_FOUND)
            )

            content = await client.get_file_contents(owner, repo, path)
            assert content is None


# --- trigger_workflow_dispatch tests ---


@pytest.fixture
def authed_client(client, mock_jwt):
    """Client with a pre-cached installation token."""
    client._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    return client


@pytest.mark.asyncio
async def test_trigger_workflow_dispatch_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.post(
                f"/repos/{owner}/{repo}/actions/workflows/deploy.yml/dispatches"
            ).mock(return_value=httpx.Response(204))

            result = await authed_client.trigger_workflow_dispatch(
                owner, repo, "deploy.yml", inputs={"env": "prod"}
            )

            assert result is True
            assert route.called
            request = route.calls[0].request
            import json

            body = json.loads(request.content)
            assert body["ref"] == "main"
            assert body["inputs"] == {"env": "prod"}


@pytest.mark.asyncio
async def test_trigger_workflow_dispatch_not_found(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.post(f"/repos/{owner}/{repo}/actions/workflows/missing.yml/dispatches").mock(
                return_value=httpx.Response(404)
            )

            with pytest.raises(httpx.HTTPStatusError):
                await authed_client.trigger_workflow_dispatch(owner, repo, "missing.yml")


@pytest.mark.asyncio
async def test_trigger_workflow_dispatch_unprocessable(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.post(f"/repos/{owner}/{repo}/actions/workflows/deploy.yml/dispatches").mock(
                return_value=httpx.Response(422)
            )

            with pytest.raises(httpx.HTTPStatusError):
                await authed_client.trigger_workflow_dispatch(owner, repo, "deploy.yml")


# --- get_latest_workflow_run tests ---


@pytest.mark.asyncio
async def test_get_latest_workflow_run_404_raises_workflow_not_found(authed_client):
    """404 on workflow runs endpoint → WorkflowNotFoundError (not raw HTTPStatusError)."""
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/actions/workflows/ci.yml/runs").mock(
                return_value=httpx.Response(404)
            )

            with pytest.raises(WorkflowNotFoundError, match="ci.yml"):
                await authed_client.get_latest_workflow_run(owner, repo, "ci.yml")


@pytest.mark.asyncio
async def test_get_latest_workflow_run_empty_runs_returns_none(authed_client):
    """200 with no runs → None (workflow exists but hasn't been triggered)."""
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/actions/workflows/ci.yml/runs").mock(
                return_value=httpx.Response(200, json={"workflow_runs": []})
            )

            result = await authed_client.get_latest_workflow_run(owner, repo, "ci.yml")
            assert result is None


# --- rerun_failed_jobs tests ---


@pytest.mark.asyncio
async def test_rerun_failed_jobs_success(authed_client):
    owner, repo, run_id = "my-org", "my-repo", 12345

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.post(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs"
            ).mock(return_value=httpx.Response(201))

            result = await authed_client.rerun_failed_jobs(owner, repo, run_id)

            assert result is True
            assert route.called


@pytest.mark.asyncio
async def test_rerun_failed_jobs_forbidden(authed_client):
    owner, repo, run_id = "my-org", "my-repo", 12345

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs").mock(
                return_value=httpx.Response(403)
            )

            with pytest.raises(httpx.HTTPStatusError):
                await authed_client.rerun_failed_jobs(owner, repo, run_id)


# --- wait_for_run_completion tests ---


@pytest.mark.asyncio
async def test_wait_for_run_completion_success(authed_client):
    owner, repo, run_id = "my-org", "my-repo", 12345

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": run_id,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{owner}/{repo}/actions/runs/{run_id}",
                    },
                )
            )

            result = await authed_client.wait_for_run_completion(
                owner, repo, run_id, timeout_seconds=10, poll_interval=1
            )

            assert result["id"] == run_id
            assert result["conclusion"] == "success"


@pytest.mark.asyncio
async def test_wait_for_run_completion_failure(authed_client):
    owner, repo, run_id = "my-org", "my-repo", 12345

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": run_id,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": f"https://github.com/{owner}/{repo}/actions/runs/{run_id}",
                    },
                )
            )

            with pytest.raises(RuntimeError, match="failed"):
                await authed_client.wait_for_run_completion(
                    owner, repo, run_id, timeout_seconds=10, poll_interval=1
                )


@pytest.mark.asyncio
async def test_wait_for_run_completion_timeout(authed_client):
    owner, repo, run_id = "my-org", "my-repo", 12345

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": run_id,
                        "status": "in_progress",
                        "conclusion": None,
                        "html_url": f"https://github.com/{owner}/{repo}/actions/runs/{run_id}",
                    },
                )
            )

            with patch("asyncio.sleep", return_value=None):
                with pytest.raises(TimeoutError, match="did not complete"):
                    await authed_client.wait_for_run_completion(
                        owner, repo, run_id, timeout_seconds=1, poll_interval=1
                    )


# --- Pull Request methods ---


@pytest.mark.asyncio
async def test_create_pull_request_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.post(f"/repos/{owner}/{repo}/pulls").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "number": 42,
                        "html_url": f"https://github.com/{owner}/{repo}/pull/42",
                        "head": {"ref": "story/abc123"},
                        "base": {"ref": "main"},
                        "node_id": "PR_abc",
                    },
                )
            )

            result = await authed_client.create_pull_request(
                owner, repo, head="story/abc123", base="main", title="Story: test"
            )

            assert result["number"] == 42
            assert route.called
            import json

            body = json.loads(route.calls[0].request.content)
            assert body["head"] == "story/abc123"
            assert body["base"] == "main"
            assert body["title"] == "Story: test"


@pytest.mark.asyncio
async def test_create_pull_request_already_exists(authed_client):
    """422 with 'already exists' should return existing PR info."""
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.post(f"/repos/{owner}/{repo}/pulls").mock(
                return_value=httpx.Response(
                    422,
                    json={
                        "message": "Validation Failed",
                        "errors": [{"message": "A pull request already exists"}],
                    },
                )
            )
            # Fallback: list PRs to find existing one
            respx_mock.get(f"/repos/{owner}/{repo}/pulls").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "number": 99,
                            "html_url": f"https://github.com/{owner}/{repo}/pull/99",
                            "head": {"ref": "story/abc123"},
                            "base": {"ref": "main"},
                            "node_id": "PR_existing",
                        }
                    ],
                )
            )

            result = await authed_client.create_pull_request(
                owner, repo, head="story/abc123", base="main", title="Story: test"
            )
            assert result["number"] == 99


@pytest.mark.asyncio
async def test_enable_auto_merge_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.post("/graphql").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "enablePullRequestAutoMerge": {
                                "pullRequest": {
                                    "number": 42,
                                    "autoMergeRequest": {"mergeMethod": "MERGE"},
                                }
                            }
                        }
                    },
                )
            )

            result = await authed_client.enable_auto_merge(owner, repo, pr_node_id="PR_abc")

            assert result is True
            assert route.called

            import json

            body = json.loads(route.calls[0].request.content)
            assert "enablePullRequestAutoMerge" in body["query"]
            assert body["variables"]["pullRequestId"] == "PR_abc"


@pytest.mark.asyncio
async def test_enable_auto_merge_not_allowed(authed_client):
    """When repo doesn't have auto-merge enabled, GraphQL returns errors."""
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.post("/graphql").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": "Pull request is not in the correct state"
                                " to enable auto-merge"
                            }
                        ]
                    },
                )
            )

            result = await authed_client.enable_auto_merge(owner, repo, pr_node_id="PR_abc")
            assert result is False


@pytest.mark.asyncio
async def test_merge_pull_request_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.put(f"/repos/{owner}/{repo}/pulls/42/merge").mock(
                return_value=httpx.Response(
                    200,
                    json={"sha": "abc123", "merged": True},
                )
            )

            result = await authed_client.merge_pull_request(owner, repo, 42)

            assert result["merged"] is True
            assert route.called

            import json

            body = json.loads(route.calls[0].request.content)
            assert body["merge_method"] == "merge"


@pytest.mark.asyncio
async def test_close_pull_request_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_installation_id", return_value=111):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.patch(f"/repos/{owner}/{repo}/pulls/42").mock(
                return_value=httpx.Response(
                    200,
                    json={"number": 42, "state": "closed"},
                )
            )

            result = await authed_client.close_pull_request(owner, repo, 42)

            assert result["state"] == "closed"
            assert route.called


# --- update_branch_protection tests ---


@pytest.mark.asyncio
async def test_update_branch_protection_success(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_org_token", return_value="org-token"):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.put(f"/repos/{owner}/{repo}/branches/main/protection").mock(
                return_value=httpx.Response(200, json={"url": "https://api.github.com/..."})
            )

            await authed_client.update_branch_protection(owner, repo, "main")

            assert route.called
            import json

            body = json.loads(route.calls[0].request.content)
            assert body["required_pull_request_reviews"]["required_approving_review_count"] == 0
            assert body["required_status_checks"]["strict"] is True
            assert "ci" in body["required_status_checks"]["contexts"]
            assert body["enforce_admins"] is False
            assert body["restrictions"] is None


@pytest.mark.asyncio
async def test_update_branch_protection_custom_checks(authed_client):
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_org_token", return_value="org-token"):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            route = respx_mock.put(f"/repos/{owner}/{repo}/branches/main/protection").mock(
                return_value=httpx.Response(200, json={})
            )

            await authed_client.update_branch_protection(
                owner, repo, "main", required_checks=["ci", "lint"], enforce_admins=True
            )

            import json

            body = json.loads(route.calls[0].request.content)
            assert body["required_status_checks"]["contexts"] == ["ci", "lint"]
            assert body["enforce_admins"] is True


@pytest.mark.asyncio
async def test_update_branch_protection_404(authed_client):
    """404 when branch doesn't exist."""
    owner, repo = "my-org", "my-repo"

    with patch.object(authed_client, "get_org_token", return_value="org-token"):
        async with respx.mock(base_url="https://api.github.com") as respx_mock:
            respx_mock.put(f"/repos/{owner}/{repo}/branches/main/protection").mock(
                return_value=httpx.Response(404)
            )

            with pytest.raises(httpx.HTTPStatusError):
                await authed_client.update_branch_protection(owner, repo, "main")
