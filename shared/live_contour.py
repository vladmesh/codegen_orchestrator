"""Which contour a live run belongs to, and the names it is allowed to own.

Production and the stand share one GitHub organization, one provider account and
one set of sweep tools. The only boundary between them is the name a live run
gives the things it creates, so that name is defined here once and read from here
everywhere — the harness that creates a project, and the sweep that deletes one.
A prefix that lived in two files could drift, and the drift would not be a failed
test: the stand's sweep would delete production's run.

The boundary is narrower than it looks. `generate_project_slug` truncates a title
before appending the project UUID, so a deployed stack keeps only the first seven
characters of the prefix:

    live-test   -> live-te-<uuid32>        stand-test  -> stand-t-<uuid32>
    live-crud   -> live-cr-<uuid32>        stand-crud  -> stand-c-<uuid32>

Two prefixes that agree on those seven characters are indistinguishable to every
sweep that works on stack names, whatever their full titles look like.
`assert_prefixes_distinct` states that requirement and the tests enforce it, so a
new contour cannot be added with a name that silently aliases an existing one.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from shared.project_slug import project_slug_prefix

CONTOUR_ENV = "LIVE_CONTOUR"
DEFAULT_CONTOUR = "prod"


@dataclass(frozen=True)
class Contour:
    """The project title prefixes one contour owns.

    `pipeline` and `crud` are what live tests create today. `legacy` names
    prefixes no test creates any more but whose leftovers a sweep must still
    recognise — dropping one would strand its orphans forever.
    """

    name: str
    pipeline: str
    crud: str
    legacy: tuple[str, ...] = ()
    #: Whether a live run may create resources in this contour. Production may
    #: not: live runs create projects, repositories and deployed stacks and then
    #: delete them, and production carries real users' data.
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
    # Production keeps the names it has always used. `mega-test` predates the
    # current harness and creates nothing today; it stays so old residue is
    # still sweepable.
    # Production keeps its names so residue from before that rule is still
    # sweepable, but no live run may create anything here.
    "prod": Contour(
        name="prod",
        pipeline="live-test",
        crud="live-crud",
        legacy=("mega-test",),
        allows_live_runs=False,
    ),
    # The stand shares production's organization, so its names must be readable
    # at a glance and distinct within the first seven characters: `stand-t-`
    # against `stand-c-`.
    "stand": Contour(name="stand", pipeline="stand-test", crud="stand-crud"),
}


def current_contour() -> Contour:
    """Return the contour this process runs in.

    Unset means production: the default has to be the contour whose behaviour
    predates this module, so an environment that never heard of it keeps working
    exactly as before.
    """
    name = os.getenv(CONTOUR_ENV) or DEFAULT_CONTOUR
    try:
        return CONTOURS[name]
    except KeyError:
        known = ", ".join(sorted(CONTOURS))
        raise ValueError(f"unknown {CONTOUR_ENV}={name!r}; known contours: {known}") from None


def require_live_contour() -> Contour:
    """Return the contour a live run may create resources in, or refuse.

    E2E belongs on the stand. A live run creates projects, repositories, servers
    and deployed stacks and then deletes them; production carries real users'
    projects and real users' data, and a sweep that matched one name too many
    there would take them with it.

    This is deliberately a refusal at creation, not at cleanup: production's
    names stay known so residue left from before this rule can still be removed.
    """
    contour = current_contour()
    if not contour.allows_live_runs:
        raise RuntimeError(
            f"live runs are not allowed in the {contour.name!r} contour: it holds real "
            f"users' data. Set {CONTOUR_ENV} to a contour that owns test resources "
            "(the stand) and run there."
        )
    return contour


def assert_prefixes_distinct(contours: list[Contour] | None = None) -> None:
    """Fail unless every prefix in every contour owns a distinct stack-name space.

    Checked across contours, not only inside one: two contours that agree on a
    truncated prefix share their deployed stacks with no way to tell whose is
    whose, and the first sweep to run deletes both.
    """
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
