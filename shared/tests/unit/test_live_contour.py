"""The contour boundary: names one contour owns cannot address another's."""

import pytest

from shared.live_contour import (
    CONTOUR_ENV,
    CONTOURS,
    Contour,
    assert_prefixes_distinct,
    current_contour,
)
from shared.project_slug import generate_project_slug, project_slug_prefix


def test_default_contour_keeps_the_historical_production_names(monkeypatch):
    """An environment that never heard of contours behaves exactly as before.

    These four strings are what production's sweep has always matched and what
    its live tests have always created. Changing one silently strands every
    resource named the old way.
    """
    monkeypatch.delenv(CONTOUR_ENV, raising=False)

    contour = current_contour()

    assert contour.name == "prod"
    assert contour.pipeline == "live-test"
    assert contour.llm_pipeline == "live-test-llm"
    assert contour.crud == "live-crud"
    assert contour.project_prefixes == ["live-test", "live-crud", "mega-test"]


def test_contour_is_selected_by_environment(monkeypatch):
    monkeypatch.setenv(CONTOUR_ENV, "stand")

    contour = current_contour()

    assert contour.name == "stand"
    assert contour.pipeline == "stand-test"
    assert contour.crud == "stand-crud"


def test_unknown_contour_is_refused_by_name(monkeypatch):
    """Fail closed: an unrecognised contour must not silently sweep production."""
    monkeypatch.setenv(CONTOUR_ENV, "staging")

    with pytest.raises(ValueError, match="unknown LIVE_CONTOUR='staging'"):
        current_contour()


def test_registered_contours_own_distinct_stack_names():
    assert_prefixes_distinct()


def test_stand_and_production_stacks_are_distinguishable():
    """The seven characters a stack name keeps must still tell the contours apart.

    This is the whole boundary while both contours share one organization: a
    sweep addresses deployed stacks by this truncated prefix and by nothing else.
    """
    prod = CONTOURS["prod"].slug_prefixes
    stand = CONTOURS["stand"].slug_prefixes

    assert prod == ["live-te-", "live-cr-", "mega-te-"]
    assert stand == ["stand-t-", "stand-c-"]
    assert not set(prod) & set(stand)


def test_aliasing_prefixes_are_refused_even_when_the_titles_differ():
    """`stand-live-test` and `stand-live-crud` are different titles and the same stack.

    Both truncate to `stand-l-`, so whichever contour swept first would delete
    the other's stacks. The check has to look at the truncated form, not the
    title, or it proves nothing.
    """
    aliasing = Contour(name="aliasing", pipeline="stand-live-test", crud="stand-live-crud")

    assert project_slug_prefix("stand-live-test") == project_slug_prefix("stand-live-crud")
    with pytest.raises(ValueError, match="claimed by both"):
        assert_prefixes_distinct([aliasing])


def test_distinctness_is_checked_across_contours_not_only_within_one():
    """A new contour that aliases production is refused at registration time."""
    impostor = Contour(name="impostor", pipeline="live-testing", crud="stand-crud")

    with pytest.raises(ValueError, match="claimed by both"):
        assert_prefixes_distinct([CONTOURS["prod"], impostor])


def test_generated_titles_carry_their_contour_prefix():
    """A real generated slug starts with the prefix the sweep looks for."""
    import uuid

    for contour in CONTOURS.values():
        for prefix, slug_prefix in zip(
            contour.project_prefixes, contour.slug_prefixes, strict=True
        ):
            slug = generate_project_slug(f"{prefix}-a1b2c3d4", uuid.uuid4())
            assert slug.startswith(slug_prefix)
