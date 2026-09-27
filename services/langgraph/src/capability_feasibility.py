"""One detection floor for PO intake and Architect admission.

The rendered data ships with src/. Matching is deliberately phrase based;
the agents must still judge requirements against the whole manifest. A term
joined by " + " matches when every one of its phrases is present.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.product_brief import ProductBriefContent


class CapabilityLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    plain: str
    why: str
    workaround: str | None
    detect: list[str]


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    cannot: list[CapabilityLimit]


_MANIFEST = _Manifest.model_validate_json(
    Path(__file__).with_name("prompts").joinpath("platform_capabilities.json").read_text()
)
MANIFEST_VERSION = _MANIFEST.version
CAPABILITY_LIMITS = {item.id: item for item in _MANIFEST.cannot}


@dataclass(frozen=True)
class CapabilityConflict:
    requirement_id: str
    requirement: str
    capability: CapabilityLimit

    @property
    def reason(self) -> str:
        item = self.capability
        return (
            f"{self.requirement_id}: {self.requirement}\n"
            f"{item.id} ({item.name}), manifest v{MANIFEST_VERSION}: {item.why} "
            f"Workaround: {item.workaround or 'No workaround is available.'}"
        )


def _term_matches(term: str, text: str) -> bool:
    return all(part in text for part in term.split(" + "))


def capability_conflicts(brief_content: ProductBriefContent) -> list[CapabilityConflict]:
    """Find unsupported requirements without an explicitly accepted workaround."""
    accepted = {choice.capability for choice in brief_content.variant_choices}
    conflicts = []
    for requirement in brief_content.must_requirements:
        text = " ".join(f"{requirement.text} {requirement.user_wording or ''}".casefold().split())
        for item in CAPABILITY_LIMITS.values():
            if item.id not in accepted and any(_term_matches(term, text) for term in item.detect):
                conflicts.append(CapabilityConflict(requirement.id, requirement.text, item))
    return conflicts


def capability_refusal(content: ProductBriefContent) -> str | None:
    """PO-facing refusal; nothing is presented or confirmed on a conflict."""
    conflicts = capability_conflicts(content)
    if not conflicts:
        return None
    return (
        "No Product Brief was presented or confirmed. No work was started.\n"
        + "\n".join(conflict.reason for conflict in conflicts)
        + '\nTell the user "not possible now" in their language, explain the workaround, '
        "and ask which they want. Only after acceptance record a variant_choices entry "
        "with capability set to that id. If they insist on the unsupported capability, "
        "use pass_capability_request; create no story."
    )
