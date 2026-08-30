"""Define live-run ownership contours and collision-free resource prefixes."""

from __future__ import annotations

from dataclasses import dataclass
import os

from shared.project_slug import project_slug_prefix

CONTOUR_ENV = "LIVE_CONTOUR"
DEFAULT_CONTOUR = "prod"


@dataclass(frozen=True)
class Contour:
    """Project title prefixes a contour may create or clean up."""

    name: str
    pipeline: str
    crud: str
    legacy: tuple[str, ...] = ()
    #: Only an owning contour may create live-run resources.
    allows_live_runs: bool = True

    @property
    def llm_pipeline(self) -> str:
        """Title prefix of the LLM pipeline projects."""
        return f"{self.pipeline}-llm"

    @property
    def project_prefixes(self) -> list[str]:
        """Every title prefix this contour owns, for creation and for sweeping."""
        return [self.pipeline, self.crud, *self.legacy]

    @property
    def slug_prefixes(self) -> list[str]:
        """The truncated forms deployed stacks are named by."""
        return [project_slug_prefix(prefix) for prefix in self.project_prefixes]


CONTOURS: dict[str, Contour] = {
    # Production prefixes remain sweepable but are never creatable by live runs.
    "prod": Contour(
        name="prod",
        pipeline="live-test",
        crud="live-crud",
        legacy=("mega-test",),
        allows_live_runs=False,
    ),
    # Stack-name slugs must remain distinct after Compose truncation.
    "stand": Contour(name="stand", pipeline="stand-test", crud="stand-crud"),
}


def current_contour() -> Contour:
    """Return the selected contour; unset remains production."""
    name = os.getenv(CONTOUR_ENV) or DEFAULT_CONTOUR
    try:
        return CONTOURS[name]
    except KeyError:
        known = ", ".join(sorted(CONTOURS))
        raise ValueError(f"unknown {CONTOUR_ENV}={name!r}; known contours: {known}") from None


def require_live_contour() -> Contour:
    """Require a contour that owns live-run resource creation and cleanup."""
    contour = current_contour()
    if not contour.allows_live_runs:
        raise RuntimeError(
            f"live runs are not allowed in the {contour.name!r} contour: it holds real "
            f"users' data. Set {CONTOUR_ENV} to a contour that owns test resources "
            "(the stand) and run there."
        )
    return contour


def assert_prefixes_distinct(contours: list[Contour] | None = None) -> None:
    """Reject contours whose truncated stack-name prefixes collide."""
    seen: dict[str, str] = {}
    for contour in contours if contours is not None else list(CONTOURS.values()):
        for prefix, slug in zip(contour.project_prefixes, contour.slug_prefixes, strict=True):
            owner = f"{contour.name}:{prefix}"
            if slug in seen:
                raise ValueError(
                    f"stack-name prefix {slug!r} is claimed by both {seen[slug]} and {owner}; "
                    "project titles must differ within the first "
                    f"{len(slug) - 1} characters"
                )
            seen[slug] = owner


assert_prefixes_distinct()
