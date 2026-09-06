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
