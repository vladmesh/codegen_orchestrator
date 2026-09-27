"""Render the platform capability manifest for people and for the prompts.

`docs/platform_capabilities.yaml` is the one source. This module validates it and renders
the two derived texts: `docs/PLATFORM_CAPABILITIES.md` for people, and the compact block
the PO and the Architect read, `services/langgraph/src/prompts/platform_capabilities.txt`
(a text file, because the langgraph image carries neither `docs/` nor Markdown).

    python -m scripts.platform_capabilities

rewrites both. Unit tests fail while either differs from what this renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "platform_capabilities.yaml"
DOCUMENT_PATH = ROOT / "docs" / "PLATFORM_CAPABILITIES.md"
PROMPT_BLOCK_PATH = ROOT / "services/langgraph/src/prompts/platform_capabilities.txt"

#: The heading the compact block starts with; the PO rule points at it by this name.
PROMPT_BLOCK_HEADING = "## Platform capabilities"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KitSource(_Model):
    source: str = Field(min_length=1)
    ref: str = Field(min_length=1)
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
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    plain: str = Field(min_length=1)
    why: str = Field(min_length=1)
    workaround: str | None = None


class KitItem(_Model):
    name: str = Field(min_length=1)
    plain: str = Field(min_length=1)


class KitPackage(KitItem):
    version: str = Field(min_length=1)


class Kit(_Model):
    modules: list[KitItem] = Field(min_length=1)
    core: list[KitItem]
    packages: list[KitPackage]


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
    status: Literal["draft", "reviewed"]
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
    kit = manifest.derived_from.kit
    return f"{kit.source}@{kit.ref}"


def render_document(manifest: CapabilityManifest) -> str:
    """The full manifest as a Markdown document people read."""
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
        "workaround where one exists. The PO and the Architect read a compact rendering of the "
        "same source on every turn.",
        "",
        f"Derived from the kit `{_kit_ref(manifest)}` (commit `{kit.commit}`). {kit.note}",
        "",
        "Code it was read from:",
        "",
        *[f"- `{path}`" for path in manifest.derived_from.code],
        "",
        "## Can",
        "",
    ]
    for item in manifest.can:
        lines += [f"### {item.name}", "", item.plain, "", f"How: {item.how}", ""]
    lines += ["## Cannot", ""]
    for item in manifest.cannot:
        lines += [f"### {item.name}", "", item.plain, "", f"Why: {item.why}", ""]
        if item.workaround:
            lines += [f"Workaround: {item.workaround}", ""]
    lines += [f"## Kit at {kit.ref}", "", "Modules:", ""]
    lines += [f"- `{module.name}`: {module.plain}" for module in manifest.kit.modules]
    lines += ["", "Core contracts every backend carries:", ""]
    lines += [f"- {core.name}: {core.plain}" for core in manifest.kit.core]
    lines += ["", "Packages:", ""]
    lines += [
        f"- `{package.name}` {package.version}: {package.plain}"
        for package in manifest.kit.packages
    ]
    lines += ["", "## Deploy targets", ""]
    for target in manifest.deploy_targets:
        flags = [
            "requestable" if target.requestable else "not requestable",
            "HTTP health check" if target.http_health else "no HTTP health check",
        ]
        lines.append(f"- `{target.service}` ({', '.join(flags)}): {target.exposure}")
    lines += ["", "## Secret kinds", ""]
    lines += [f"- `{kind.source}`: {kind.plain}" for kind in manifest.secret_kinds]
    lines += [
        "",
        "## Derived keys",
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
    """The compact block appended to the PO's and the Architect's model input."""
    kit = manifest.kit
    lines = [
        f"{PROMPT_BLOCK_HEADING} (manifest v{manifest.version}, {manifest.status})",
        f"What a product built here can and cannot have, kit {_kit_ref(manifest)}.",
        "Can:",
        *[f"- {item.name}: {item.plain}" for item in manifest.can],
        "Cannot (why; instead):",
    ]
    for item in manifest.cannot:
        line = f"- {item.name}: {' '.join(item.why.split())}"
        if item.workaround:
            line += f" Instead: {' '.join(item.workaround.split())}"
        lines.append(line)
    lines += [
        "Kit modules: " + "; ".join(f"{m.name}: {_clause(m.plain)}" for m in kit.modules) + ".",
        "Kit packages: "
        + "; ".join(f"{p.name} {p.version}: {_clause(p.plain)}" for p in kit.packages)
        + ".",
        "Secret kinds: " + ", ".join(kind.source for kind in manifest.secret_kinds) + ".",
        "Derived keys (no others exist): "
        + ", ".join(derived.key for derived in manifest.derived_keys)
        + ".",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    manifest = load_manifest()
    DOCUMENT_PATH.write_text(render_document(manifest))
    PROMPT_BLOCK_PATH.write_text(render_prompt_block(manifest))


if __name__ == "__main__":
    main()
