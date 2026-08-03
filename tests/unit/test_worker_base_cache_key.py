"""A derived worker image must not keep its tag when the common image changes.

The DinD fixture skips a build when the tag already exists, so the tag is the whole
cache key. If the common image's hash does not reach it, a rebuilt common leaves the
old child in place and gets retagged :latest, and the BASE_IMAGE the fixture passes is
never used.
"""

from pathlib import Path

from tests.integration.backend.conftest import _child_image_hash, _content_hash

CLAUDE_DOCKERFILE = (
    Path(__file__).parents[2] / "services/worker-manager/images/worker-base-claude/Dockerfile"
)


def test_a_new_common_hash_gives_the_child_a_new_tag():
    dockerfile = str(CLAUDE_DOCKERFILE)

    assert _child_image_hash(dockerfile, "aaaaaaaaaaaa") != _child_image_hash(
        dockerfile, "bbbbbbbbbbbb"
    )


def test_the_same_inputs_give_the_same_tag():
    dockerfile = str(CLAUDE_DOCKERFILE)

    assert _child_image_hash(dockerfile, "aaaaaaaaaaaa") == _child_image_hash(
        dockerfile, "aaaaaaaaaaaa"
    )


def test_a_changed_child_dockerfile_gives_a_new_tag(tmp_path):
    first = tmp_path / "Dockerfile"
    first.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n")
    before = _child_image_hash(str(first), "aaaaaaaaaaaa")
    first.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER worker\n")

    assert _child_image_hash(str(first), "aaaaaaaaaaaa") != before


def test_the_common_hash_is_not_swallowed_as_a_path(tmp_path):
    """The defect this replaces: _content_hash reads paths, so a hash string vanished."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12.13-slim\n")

    assert _content_hash(str(dockerfile), "aaaaaaaaaaaa") == _content_hash(
        str(dockerfile), "bbbbbbbbbbbb"
    )
    assert _child_image_hash(str(dockerfile), "aaaaaaaaaaaa") != _child_image_hash(
        str(dockerfile), "bbbbbbbbbbbb"
    )
