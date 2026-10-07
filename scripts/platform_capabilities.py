"""Render the platform capability manifest for people and for the prompts.

`docs/platform_capabilities.yaml` is the one source. This module validates it and renders
the derived texts: `docs/PLATFORM_CAPABILITIES.md` for people, the compact product block
the PO reads, `services/langgraph/src/prompts/platform_capabilities.txt`, and the technical
block the Architect reads, `platform_capabilities_architect.txt` beside it (the langgraph
image carries neither `docs/` nor Markdown). Beside them, `platform_capabilities.json`
carries the version and the product-language detection data used at runtime.

    python -m scripts.platform_capabilities

rewrites all four. Unit tests fail while any differs from what this renders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "platform_capabilities.yaml"
DOCUMENT_PATH = ROOT / "docs" / "PLATFORM_CAPABILITIES.md"
PROMPT_BLOCK_PATH = ROOT / "services/langgraph/src/prompts/platform_capabilities.txt"
ARCHITECT_BLOCK_PATH = PROMPT_BLOCK_PATH.with_name("platform_capabilities_architect.txt")
RUNTIME_PATH = PROMPT_BLOCK_PATH.with_suffix(".json")

#: The heading the PO's product block starts with; the PO rule points at it by this name.
PROMPT_BLOCK_HEADING = "## Platform capabilities"
#: The heading the Architect's technical block starts with.
ARCHITECT_BLOCK_HEADING = "## Platform capabilities, technical"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KitSource(_Model):
    source: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    note: str = Field(min_length=1)


class DerivedFrom(_Model):
    kit: KitSource
    code: list[str] = Field(min_length=1)


class Capability(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    plain: str = Field(min_length=1)
    how: str = Field(min_length=1)


class Limitation(_Model):
    """One thing a product cannot have.

    `name`, `plain`, `why` and `workaround` are product language: they reach the PO and,
    through a refusal, the user. `technical` reaches only the Architect and the document.
    `aliases` are retired ids merged into this one, still honoured in recorded choices.
    A `detect` term always trips; a `detect_weak` term trips unless the requirement also
    carries a `weak_unless` phrase, such as an expense tracker merely recording payments.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    aliases: list[str] = []
    name: str = Field(min_length=1)
    plain: str = Field(min_length=1)
    why: str = Field(min_length=1)
    workaround: str | None = None
    technical: str = Field(min_length=1)
    detect: list[str] = Field(min_length=1)
    detect_weak: list[str] = []
    weak_unless: list[str] = []


class KitItem(_Model):
    name: str = Field(min_length=1)
    plain: str = Field(min_length=1)


class Kit(_Model):
    modules: list[KitItem] = Field(min_length=1)
    core: list[KitItem]
    #: Where packages come from. They are not listed here: the Architect reads the kit's
    #: live catalog at planning time (`services/langgraph/src/kit_catalog.py`).
    catalog: str = Field(min_length=1)


class DeployTarget(_Model):
    service: str = Field(min_length=1)
    requestable: bool
    http_health: bool
    exposure: str = Field(min_length=1)


class SecretKind(_Model):
    source: str = Field(min_length=1)
    plain: str = Field(min_length=1)


class DerivedKey(_Model):
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class UnresolvedKitKey(_Model):
    key: str = Field(min_length=1)
    note: str = Field(min_length=1)


class CapabilityManifest(_Model):
    """The manifest as validated data; an unknown or missing field fails the load."""

    version: int = Field(ge=1)
    status: Literal["draft", "reviewed", "owner-reviewed"]
    review: str = Field(min_length=1)
    derived_from: DerivedFrom
    can: list[Capability] = Field(min_length=1)
    cannot: list[Limitation] = Field(min_length=1)
    kit: Kit
    deploy_targets: list[DeployTarget] = Field(min_length=1)
    secret_kinds: list[SecretKind] = Field(min_length=1)
    derived_keys: list[DerivedKey] = Field(min_length=1)
    kit_derived_keys_not_resolved: list[UnresolvedKitKey]


def load_manifest(path: Path = MANIFEST_PATH) -> CapabilityManifest:
    """Read and validate the manifest, failing loudly on anything malformed."""
    return CapabilityManifest.model_validate(yaml.safe_load(path.read_text()))


def _status_line(manifest: CapabilityManifest) -> str:
    return f"Version {manifest.version}, status: {manifest.status} ({manifest.review})."


def _kit_ref(manifest: CapabilityManifest) -> str:
    """The kit as source and short commit; the release tag lives only in the pin's seed."""
    kit = manifest.derived_from.kit
    return f"{kit.source}@{kit.commit[:12]}"


def _product_lines(manifest: CapabilityManifest) -> list[str]:
    """Can / cannot / instead, in product language only."""
    lines = ["Can:", *[f"- {item.name}: {_clause(item.plain)}." for item in manifest.can]]
    lines.append("Cannot (why; instead):")
    for item in manifest.cannot:
        line = f"- {item.name}: [{item.id}] {_clause(item.plain)}. {_clause(item.why)}."
        line += (
            f" Instead: {_clause(item.workaround)}." if item.workaround else " No way around it."
        )
        lines.append(line)
    return lines


def render_document(manifest: CapabilityManifest) -> str:
    """The full manifest as a Markdown document people read, the product part first."""
    kit = manifest.derived_from.kit
    lines = [
        "# Platform capabilities",
        "",
        "<!-- Generated from docs/platform_capabilities.yaml by "
        "`python -m scripts.platform_capabilities`; edit the YAML, not this file. -->",
        "",
        f"**{_status_line(manifest)}**",
        "",
        "What a product built by this orchestrator can have and what it cannot, with the "
        "workaround where one exists. The PO reads the product part of the same source on "
        "every turn; the Architect reads the technical part.",
        "",
        "## For the product owner",
        "",
        "### Can",
        "",
    ]
    lines += [f"- **{item.name}.** {' '.join(item.plain.split())}" for item in manifest.can]
    lines += ["", "### Cannot", ""]
    for item in manifest.cannot:
        instead = (
            f" Instead: {' '.join(item.workaround.split())}"
            if item.workaround
            else " There is no way around it."
        )
        lines.append(
            f"- **{item.name}** (`{item.id}`). {' '.join(item.plain.split())} "
            f"{' '.join(item.why.split())}{instead}"
        )
    lines += [
        "",
        "## Technical detail",
        "",
        f"Derived from the kit `{kit.source}` at commit `{kit.commit}`. {kit.note}",
        "",
        "Code it was read from:",
        "",
        *[f"- `{path}`" for path in manifest.derived_from.code],
        "",
        "### How each capability works",
        "",
    ]
    for item in manifest.can:
        lines += [f"#### {item.name}", "", f"How: {' '.join(item.how.split())}", ""]
    lines += ["### Why each limitation holds", ""]
    for item in manifest.cannot:
        lines += [f"#### {item.name}", "", f"Why: {' '.join(item.technical.split())}", ""]
        if item.aliases:
            lines += [
                "Merges the former ids " + ", ".join(f"`{alias}`" for alias in item.aliases) + ".",
                "",
            ]
    lines += [f"### Kit at {kit.commit[:12]}", "", "Modules:", ""]
    lines += [f"- `{module.name}`: {module.plain}" for module in manifest.kit.modules]
    lines += ["", "Core contracts every backend carries:", ""]
    lines += [f"- {core.name}: {core.plain}" for core in manifest.kit.core]
    lines += ["", f"Packages, from the catalog: {' '.join(manifest.kit.catalog.split())}"]
    lines += ["", "### Deploy targets", ""]
    for target in manifest.deploy_targets:
        flags = [
            "requestable" if target.requestable else "not requestable",
            "HTTP health check" if target.http_health else "no HTTP health check",
        ]
        lines.append(f"- `{target.service}` ({', '.join(flags)}): {target.exposure}")
    lines += ["", "### Secret kinds", ""]
    lines += [f"- `{kind.source}`: {kind.plain}" for kind in manifest.secret_kinds]
    lines += [
        "",
        "### Derived keys",
        "",
        "The only `derived` keys a deploy can fill. Any other derived key is left out when it "
        "is optional and fails the deploy when it is required.",
        "",
    ]
    lines += [f"- `{derived.key}`: {derived.value}" for derived in manifest.derived_keys]
    if manifest.kit_derived_keys_not_resolved:
        lines += ["", "Declared by the kit for production but not computed:", ""]
        lines += [
            f"- `{unresolved.key}`: {unresolved.note}"
            for unresolved in manifest.kit_derived_keys_not_resolved
        ]
    return "\n".join(_escape_markdown(line) for line in lines) + "\n"


def _escape_markdown(line: str) -> str:
    """Keep a literal `<placeholder>` visible: Markdown would read it as an HTML tag.

    Code spans (between backticks) print as written, and the generated-file comment
    is HTML on purpose, so both are left alone.
    """
    if line.startswith("<!--"):
        return line
    parts = line.split("`")
    parts[::2] = [part.replace("<", "\\<") for part in parts[::2]]
    return "`".join(parts)


def _clause(text: str) -> str:
    """One line of prose without its closing full stop, for joining into a list."""
    return " ".join(text.split()).removesuffix(".")


def render_prompt_block(manifest: CapabilityManifest) -> str:
    """The PO's compact product block: can, cannot and instead, nothing technical."""
    lines = [
        f"{PROMPT_BLOCK_HEADING} (manifest v{manifest.version}, {manifest.status})",
        "What a product built here can and cannot have. The product is a Telegram bot "
        "and has no web address.",
        *_product_lines(manifest),
    ]
    return "\n".join(lines) + "\n"


def render_architect_block(manifest: CapabilityManifest) -> str:
    """The Architect's block: the same list with how and why it holds in the code."""
    kit = manifest.kit
    lines = [
        f"{ARCHITECT_BLOCK_HEADING} (manifest v{manifest.version}, {manifest.status})",
        f"Kit {_kit_ref(manifest)}.",
        "Can (how):",
        *[f"- {item.name} [{item.id}]: {' '.join(item.how.split())}" for item in manifest.can],
        "Cannot (why in the code; instead):",
    ]
    for item in manifest.cannot:
        line = f"- {item.name}: [{item.id}] {' '.join(item.technical.split())}"
        if item.workaround:
            line += f" Instead: {' '.join(item.workaround.split())}"
        lines.append(line)
    lines += [
        "Kit modules: " + "; ".join(f"{m.name}: {_clause(m.plain)}" for m in kit.modules) + ".",
        "Kit core: " + "; ".join(f"{c.name}: {_clause(c.plain)}" for c in kit.core) + ".",
        f"Kit packages: {_clause(kit.catalog)}.",
        "Secret kinds: " + ", ".join(kind.source for kind in manifest.secret_kinds) + ".",
        "Derived keys (no others exist): "
        + ", ".join(derived.key for derived in manifest.derived_keys)
        + ".",
    ]
    return "\n".join(lines) + "\n"


def render_runtime(manifest: CapabilityManifest) -> str:
    """Runtime detection data, product fields only; the service image does not carry docs/."""
    return (
        json.dumps(
            {
                "version": manifest.version,
                "cannot": [item.model_dump(exclude={"technical"}) for item in manifest.cannot],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    manifest = load_manifest()
    DOCUMENT_PATH.write_text(render_document(manifest))
    PROMPT_BLOCK_PATH.write_text(render_prompt_block(manifest))
    ARCHITECT_BLOCK_PATH.write_text(render_architect_block(manifest))
    RUNTIME_PATH.write_text(render_runtime(manifest))


if __name__ == "__main__":
    main()
