"""Project slug generation tests."""

import uuid

import pytest

from shared.project_slug import PROJECT_SLUG_PATTERN, generate_project_slug


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Cool App", "my-cool-00000000111122223333444444444444"),
        ("Привет мир", "p-00000000111122223333444444444444"),
        ("!!!", "p-00000000111122223333444444444444"),
        ("123 App", "p-123-a-00000000111122223333444444444444"),
        ("A" * 80, f"{'a' * 7}-00000000111122223333444444444444"),
    ],
)
def test_generate_project_slug_matches_contract(title, expected):
    project_id = uuid.UUID("00000000-1111-2222-3333-444444444444")

    slug = generate_project_slug(title, project_id)

    assert slug == expected
    assert len(slug) <= 40  # noqa: PLR2004
    assert PROJECT_SLUG_PATTERN.fullmatch(slug)


def test_generate_project_slug_uses_enough_uuid_bits_to_avoid_prefix_collision():
    title = "Same Title"

    first = generate_project_slug(title, uuid.UUID("abcd0000-0000-0000-0000-000000000001"))
    second = generate_project_slug(title, uuid.UUID("abcdffff-ffff-ffff-ffff-ffffffffffff"))

    assert first != second
