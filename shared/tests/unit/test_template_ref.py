"""The template pin is immutable, and Copier's record of a render is matched against it."""

from pydantic import TypeAdapter, ValidationError
import pytest

from shared.contracts.template import ServiceTemplateRef, recorded_template_commit_matches

COMMIT = "52e9107495949c9187f41cc0e50367ed8ed7a7a1"
REF = TypeAdapter(ServiceTemplateRef)


@pytest.mark.parametrize("ref", ["0.10.1", COMMIT])
def test_a_release_tag_or_a_full_commit_is_a_pin(ref: str) -> None:
    assert REF.validate_python(ref) == ref


@pytest.mark.parametrize("ref", ["HEAD", "main", "master"])
def test_a_floating_ref_is_refused(ref: str) -> None:
    with pytest.raises(ValidationError, match="immutable tag or commit"):
        REF.validate_python(ref)


@pytest.mark.parametrize(
    "recorded",
    [COMMIT, "packages/tg-channels/v0.1.2-12-g52e9107", f"0.10.1-3-g{COMMIT[:12]}"],
)
def test_a_commit_pin_matches_its_own_and_its_described_record(recorded: str) -> None:
    assert recorded_template_commit_matches(recorded, COMMIT)


@pytest.mark.parametrize(
    "recorded",
    [
        "packages/tg-channels/v0.1.2-12-g52e9108",
        "packages/tg-channels/v0.1.2",
        "0.10.1",
        "1" * 40,
        "g52e9107",
    ],
)
def test_another_render_does_not_match_a_commit_pin(recorded: str) -> None:
    assert not recorded_template_commit_matches(recorded, COMMIT)


def test_a_tag_pin_matches_only_itself() -> None:
    assert recorded_template_commit_matches("0.10.1", "0.10.1")
    assert not recorded_template_commit_matches("0.10.1-1-g52e9107", "0.10.1")
