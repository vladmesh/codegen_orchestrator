"""The image tag and reference contract the deploy path shares with the project's CI."""

import httpx
import pytest

from shared.clients import registry
from shared.clients.registry import (
    DockerRegistryClient,
    ImageReference,
    parse_image_reference,
    sha_image_tag,
)

HEAD_SHA = "6e2fd5b4" + "0" * 32


def test_sha_tag_is_what_metadata_actions_type_sha_publishes():
    """`type=sha` defaults to `prefix=sha-`, `suffix=`, `format=short` (7 hex chars).

    The generated project's CI declares `type=sha` with no attributes, so this
    exact string is what `build-and-push` pushes for the merged commit. A near
    miss here is a deploy that cannot pull at all.
    """
    assert sha_image_tag(HEAD_SHA) == "sha-6e2fd5b"


def test_sha_tag_is_case_insensitive_about_the_commit():
    assert sha_image_tag(HEAD_SHA.upper()) == sha_image_tag(HEAD_SHA)


@pytest.mark.parametrize("value", ["", "main", "6e2fd5b4", "z" * 40])
def test_a_tag_is_only_derived_from_a_full_commit_sha(value):
    with pytest.raises(ValueError, match="full commit SHA"):
        sha_image_tag(value)


def test_reference_splits_into_the_parts_a_registry_read_needs():
    assert parse_image_reference("registry.example.com/my-org/my-repo-backend:sha-6e2fd5b") == (
        ImageReference(
            registry="registry.example.com",
            repository="my-org/my-repo-backend",
            tag="sha-6e2fd5b",
        )
    )


@pytest.mark.parametrize(
    "value",
    [
        "registry.example.com/my-org/my-repo-backend",
        "my-repo-backend:sha-6e2fd5b",
        ":sha-6e2fd5b",
    ],
)
def test_an_untagged_or_hostless_reference_is_malformed_rather_than_defaulted(value):
    """`:latest` by omission is exactly the behaviour this module exists to remove."""
    with pytest.raises(ValueError):
        parse_image_reference(value)


def _registry_answering(monkeypatch, handler) -> DockerRegistryClient:
    """A client whose reads are served by `handler` instead of the network.

    The credentials are the ones `registry_credentials` reads, so the client is
    constructed exactly as the deploy path constructs it.
    """
    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(registry.httpx, "AsyncClient", factory)
    return DockerRegistryClient()


@pytest.mark.asyncio
async def test_a_published_image_answers_with_its_digest(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/my-org/my-repo-backend/manifests/sha-6e2fd5b"
        return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:abc"})

    client = _registry_answering(monkeypatch, handler)
    assert await client.manifest_digest("my-org/my-repo-backend", "sha-6e2fd5b") == "sha256:abc"


@pytest.mark.asyncio
async def test_an_absent_image_is_absent_rather_than_an_error(monkeypatch):
    """404 is the one status that answers the gate's question with "not there"."""
    client = _registry_answering(monkeypatch, lambda request: httpx.Response(404))
    assert await client.manifest_digest("my-org/my-repo-backend", "sha-6e2fd5b") is None


@pytest.mark.parametrize("status", [401, 403, 500, 502])
@pytest.mark.asyncio
async def test_any_other_status_is_the_read_failing_and_keeps_that_name(monkeypatch, status):
    """A failed read must not reach the deploy as "not published", nor untyped.

    The deploy routes `RegistryError` to IMAGE_REGISTRY_UNREADABLE; an
    `httpx.HTTPStatusError` would escape the gate's typed catch and be reported
    as a generic deploy failure instead.
    """
    client = _registry_answering(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(registry.RegistryError, match=f"HTTP {status}"):
        await client.manifest_digest("my-org/my-repo-backend", "sha-6e2fd5b")


@pytest.mark.asyncio
async def test_a_manifest_without_a_digest_is_not_a_published_image(monkeypatch):
    client = _registry_answering(monkeypatch, lambda request: httpx.Response(200))
    with pytest.raises(registry.RegistryError, match="no digest"):
        await client.manifest_digest("my-org/my-repo-backend", "sha-6e2fd5b")


@pytest.mark.asyncio
async def test_an_unreachable_registry_is_not_an_absent_image(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to registry")

    client = _registry_answering(monkeypatch, handler)
    with pytest.raises(registry.RegistryError, match="could not be read"):
        await client.manifest_digest("my-org/my-repo-backend", "sha-6e2fd5b")
