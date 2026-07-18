"""Project slug generation tests."""

import uuid

import pytest

from shared.project_slug import PROJECT_SLUG_PATTERN, generate_project_slug


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Cool App", "my-cool-app-0000"),
        ("Привет мир", "p-0000"),
        ("!!!", "p-0000"),
        ("123 App", "p-123-app-0000"),
        ("A" * 80, f"{'a' * 35}-0000"),
    ],
)
def test_generate_project_slug_matches_contract(title, expected):
    project_id = uuid.UUID("00000000-1111-2222-3333-444444444444")

    slug = generate_project_slug(title, project_id)

    assert slug == expected
    assert len(slug) <= 40  # noqa: PLR2004
    assert PROJECT_SLUG_PATTERN.fullmatch(slug)
