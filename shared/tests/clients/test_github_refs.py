"""Git ref operations used to pin a deploy to one commit."""

from datetime import UTC, datetime, timedelta
import json
import os
from unittest.mock import patch

import httpx
import pytest
import respx

from shared.clients.github import DEPLOY_PIN_TAG_PREFIX, GitHubAppClient, deploy_pin_tag

SHA = "a" * 40
OTHER_SHA = "b" * 40


@pytest.fixture
def authed_client():
    with patch.dict(
        os.environ, {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY_PATH": "dummy.pem"}
    ):
        client = GitHubAppClient()
    client._private_key = "dummy_private_key"
    client._token_cache[111] = ("token", datetime.now(UTC) + timedelta(hours=1))
    with (
        patch.object(GitHubAppClient, "_generate_jwt", return_value="mock_jwt"),
        patch.object(client, "get_installation_id", return_value=111),
    ):
        yield client


class TestDeployPinTagName:
    def test_same_commit_always_yields_the_same_tag(self):
        assert deploy_pin_tag(SHA) == deploy_pin_tag(SHA.upper())

    def test_tag_is_recognizable_as_a_service_tag_and_names_its_commit(self):
        tag = deploy_pin_tag(SHA)

        assert tag.startswith(DEPLOY_PIN_TAG_PREFIX)
        assert tag[len(DEPLOY_PIN_TAG_PREFIX) :] == SHA

    @pytest.mark.parametrize("value", ["", "main", "aaaaaaa", f"refs/tags/{SHA}"])
    def test_anything_but_a_full_sha_is_rejected(self, value):
        with pytest.raises(ValueError, match="full commit SHA"):
            deploy_pin_tag(value)


@pytest.mark.asyncio
async def test_create_or_reset_tag_creates_the_ref_at_the_commit(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        route = mock.post("/repos/o/r/git/refs").mock(return_value=httpx.Response(201, json={}))

        await authed_client.create_or_reset_tag("o", "r", "pin-tag", SHA)

        body = json.loads(route.calls[0].request.content)
        assert body == {"ref": "refs/tags/pin-tag", "sha": SHA}


@pytest.mark.asyncio
async def test_create_or_reset_tag_moves_a_tag_left_over_from_a_crashed_deploy(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        mock.post("/repos/o/r/git/refs").mock(
            return_value=httpx.Response(422, json={"message": "Reference already exists"})
        )
        patch_route = mock.patch("/repos/o/r/git/refs/tags/pin-tag").mock(
            return_value=httpx.Response(200, json={})
        )

        await authed_client.create_or_reset_tag("o", "r", "pin-tag", SHA)

        body = json.loads(patch_route.calls[0].request.content)
        assert body == {"sha": SHA, "force": True}


@pytest.mark.asyncio
async def test_create_or_reset_tag_propagates_other_errors(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        mock.post("/repos/o/r/git/refs").mock(return_value=httpx.Response(404))

        with pytest.raises(httpx.HTTPStatusError):
            await authed_client.create_or_reset_tag("o", "r", "pin-tag", SHA)


@pytest.mark.asyncio
async def test_delete_ref_removes_the_tag(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        route = mock.delete("/repos/o/r/git/refs/tags/pin-tag").mock(
            return_value=httpx.Response(204)
        )

        assert await authed_client.delete_ref("o", "r", "tags/pin-tag") is True
        assert route.called


@pytest.mark.asyncio
async def test_delete_ref_reports_an_already_absent_ref(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        mock.delete("/repos/o/r/git/refs/tags/pin-tag").mock(return_value=httpx.Response(404))

        assert await authed_client.delete_ref("o", "r", "tags/pin-tag") is False


@pytest.mark.asyncio
async def test_get_ref_sha_reads_the_commit_behind_a_tag(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/o/r/git/ref/tags/pin-tag").mock(
            return_value=httpx.Response(200, json={"object": {"sha": OTHER_SHA}})
        )

        assert await authed_client.get_ref_sha("o", "r", "tags/pin-tag") == OTHER_SHA


@pytest.mark.asyncio
async def test_get_ref_sha_is_none_for_a_missing_ref(authed_client):
    async with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/o/r/git/ref/tags/pin-tag").mock(return_value=httpx.Response(404))

        assert await authed_client.get_ref_sha("o", "r", "tags/pin-tag") is None
