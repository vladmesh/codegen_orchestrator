"""One detection floor for PO intake and Architect admission.

The rendered data ships with src/ and carries only the product-language fields,
so a refusal never quotes technical detail. Matching is deliberately phrase based;
the agents must still judge requirements against the whole manifest. A term
joined by " + " matches when every one of its phrases is present, and "ё" reads
as "е" on both sides. A weak term (a bare word such as "payment") trips only
when none of the limitation's `weak_unless` phrases is in the requirement.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.product_brief import ProductBriefContent


class CapabilityLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    aliases: list[str]
    name: str
    plain: str
    why: str
    workaround: str | None
    detect: list[str]
    detect_weak: list[str]
    weak_unless: list[str]

    def trips(self, text: str) -> bool:
        """Whether a normalised requirement text needs this missing capability."""
        if any(_term_matches(term, text) for term in self.detect):
            return True
        return any(_term_matches(term, text) for term in self.detect_weak) and not any(
            _normalise(phrase) in text for phrase in self.weak_unless
        )


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
            f"{item.id} ({item.name}), manifest v{MANIFEST_VERSION}: {item.plain} {item.why} "
            f"Workaround: {item.workaround or 'No workaround is available.'}"
        )


def _normalise(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _term_matches(term: str, text: str) -> bool:
    return all(_normalise(part) in text for part in term.split(" + "))


def capability_conflicts(brief_content: ProductBriefContent) -> list[CapabilityConflict]:
    """Find unsupported requirements without an explicitly accepted workaround.

    A choice naming a retired id merged into a limitation still waives it.
    """
    accepted = {choice.capability for choice in brief_content.variant_choices}
    conflicts = []
    for requirement in brief_content.must_requirements:
        text = _normalise(f"{requirement.text} {requirement.user_wording or ''}")
        for item in CAPABILITY_LIMITS.values():
            waived = not accepted.isdisjoint({item.id, *item.aliases})
            if not waived and item.trips(text):
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
