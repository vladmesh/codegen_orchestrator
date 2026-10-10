"""The template pin is immutable, and Copier's record of a render is matched against it."""

from pydantic import TypeAdapter, ValidationError
import pytest

from shared.contracts.template import ServiceTemplateRef, recorded_template_commit_matches

#: Any full commit: these rules are about the shape of a pin, not about the current one.
COMMIT = "0123456789abcdef0123456789abcdef01234567"
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
    [COMMIT, "packages/tg-channels/v0.1.2-12-g0123456", f"0.10.1-3-g{COMMIT[:12]}"],
)
def test_a_commit_pin_matches_its_own_and_its_described_record(recorded: str) -> None:
    assert recorded_template_commit_matches(recorded, COMMIT)


@pytest.mark.parametrize(
    "recorded",
    [
        "packages/tg-channels/v0.1.2-12-g0123457",
        "packages/tg-channels/v0.1.2",
        "0.10.1",
        "1" * 40,
        "g0123456",
    ],
)
def test_another_render_does_not_match_a_commit_pin(recorded: str) -> None:
    assert not recorded_template_commit_matches(recorded, COMMIT)


def test_a_tag_pin_matches_only_itself() -> None:
    assert recorded_template_commit_matches("0.10.1", "0.10.1")
    assert not recorded_template_commit_matches("0.10.1-1-g0123456", "0.10.1")
