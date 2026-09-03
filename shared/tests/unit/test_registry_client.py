"""The image tag and reference contract the deploy path shares with the project's CI."""

import pytest

from shared.clients.registry import ImageReference, parse_image_reference, sha_image_tag

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
