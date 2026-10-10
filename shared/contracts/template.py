import re
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints

# The admitted template sources: owner-controlled repositories only. The literal is
# what bounds the scaffolder's Copier invocation, so widening it is the decision to
# run that repository's Copier tasks.
ServiceTemplateSource = Literal[
    "gh:vladmesh/service-template",
    "gh:vladmesh/codegen-product-kit",
]


def _reject_floating_ref(value: str) -> str:
    if value.lower() in {"head", "main", "master"}:
        raise ValueError("template_ref must identify an immutable tag or commit")
    return value


ServiceTemplateRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    ),
    AfterValidator(_reject_floating_ref),
]


#: The `git describe` form Copier records as `_commit` when it renders a commit that no tag
#: names: `<nearest tag>-<distance>-g<abbreviated commit>`.
_DESCRIBED_COMMIT = re.compile(r"^.+-\d+-g(?P<abbreviated>[0-9a-f]{7,40})$")
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def recorded_template_commit_matches(recorded: str, pin: str) -> bool:
    """Whether Copier's recorded `_commit` is the render of the template pin `pin`.

    A tag pin is recorded as itself. A commit pin is recorded as the commit, or, when a tag
    is reachable from it, in the `git describe` form whose abbreviated commit is a prefix of
    the pinned one.
    """
    if recorded == pin:
        return True
    described = _DESCRIBED_COMMIT.fullmatch(recorded)
    return bool(
        _FULL_COMMIT.fullmatch(pin) and described and pin.startswith(described["abbreviated"])
    )
