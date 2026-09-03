"""The bounded, fail-closed wait for the merged commit's published images."""

import pytest

from shared.clients.registry import sha_image_tag
from src.subgraphs.devops.image_gate import (
    ImagesNotPublishedError,
    image_references,
    wait_for_published_images,
)

HEAD_SHA = "6e2fd5b4" + "0" * 32
TAG = sha_image_tag(HEAD_SHA)
BACKEND = f"registry.example.com/my-org/my-repo-backend:{TAG}"
TG_BOT = f"registry.example.com/my-org/my-repo-tg-bot:{TAG}"
DIGEST = "sha256:" + "a" * 64


class FakeRegistry:
    """A registry that publishes each repository after a set number of reads."""

    def __init__(self, published: dict[str, int]):
        self._published = published
        self.reads: list[tuple[str, str]] = []

    async def manifest_digest(self, repository: str, tag: str) -> str | None:
        self.reads.append((repository, tag))
        remaining = self._published.get(repository)
        if remaining is None:
            return None
        if remaining > 0:
            self._published[repository] = remaining - 1
            return None
        return DIGEST


def test_image_references_are_read_back_from_the_resolved_environment():
    values = {"BACKEND_IMAGE": BACKEND, "DB_HOST": "localhost", "TG_BOT_IMAGE": TG_BOT}
    assert image_references(values) == {"BACKEND_IMAGE": BACKEND, "TG_BOT_IMAGE": TG_BOT}


@pytest.mark.asyncio
async def test_published_images_pass_the_gate_with_their_digests():
    registry = FakeRegistry({"my-org/my-repo-backend": 0, "my-org/my-repo-tg-bot": 0})

    digests = await wait_for_published_images(
        {"BACKEND_IMAGE": BACKEND, "TG_BOT_IMAGE": TG_BOT},
        timeout_seconds=30,
        poll_seconds=0,
        registry=registry,
    )

    assert digests == {"BACKEND_IMAGE": DIGEST, "TG_BOT_IMAGE": DIGEST}
    assert ("my-org/my-repo-backend", TAG) in registry.reads


@pytest.mark.asyncio
async def test_an_image_that_arrives_late_is_waited_for():
    """The whole point of the bound is that CI is allowed to still be building."""
    registry = FakeRegistry({"my-org/my-repo-backend": 0, "my-org/my-repo-tg-bot": 2})

    digests = await wait_for_published_images(
        {"BACKEND_IMAGE": BACKEND, "TG_BOT_IMAGE": TG_BOT},
        timeout_seconds=30,
        poll_seconds=0,
        registry=registry,
    )

    assert digests == {"BACKEND_IMAGE": DIGEST, "TG_BOT_IMAGE": DIGEST}
    # A repository that already answered is not asked again.
    assert registry.reads.count(("my-org/my-repo-backend", TAG)) == 1


@pytest.mark.asyncio
async def test_an_image_that_never_appears_refuses_within_the_bound():
    registry = FakeRegistry({"my-org/my-repo-backend": 0})

    with pytest.raises(ImagesNotPublishedError) as refusal:
        await wait_for_published_images(
            {"BACKEND_IMAGE": BACKEND, "TG_BOT_IMAGE": TG_BOT},
            timeout_seconds=0,
            poll_seconds=0,
            registry=registry,
        )

    assert TG_BOT in str(refusal.value)
    assert BACKEND not in str(refusal.value)


@pytest.mark.asyncio
async def test_a_deploy_that_named_no_images_is_refused_rather_than_waved_through():
    with pytest.raises(ImagesNotPublishedError):
        await wait_for_published_images({}, timeout_seconds=0, poll_seconds=0, registry=None)


@pytest.mark.asyncio
async def test_a_registry_that_cannot_be_read_is_not_reported_as_unpublished():
    """Not asked and not there are different answers; only one may refuse quietly."""

    class BrokenRegistry:
        async def manifest_digest(self, repository: str, tag: str) -> str | None:
            raise RuntimeError("registry could not be read")

    with pytest.raises(RuntimeError) as error:
        await wait_for_published_images(
            {"BACKEND_IMAGE": BACKEND},
            timeout_seconds=30,
            poll_seconds=0,
            registry=BrokenRegistry(),
        )

    assert not isinstance(error.value, ImagesNotPublishedError)
