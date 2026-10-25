"""A product-local kit package INSTALL target and its probe evidence."""

from typing import Annotated, Literal
import unicodedata
import uuid

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from shared.contracts.dto.catalog_install import (
    SHA,
    CatalogInstall,
    Digest,
    Distribution,
    InstallPreflight,
    Name,
    Strict,
    Version,
)


def _product_relative(path: str) -> str:
    """Lexical only: the executor checks the owned checkout and the kit physical containment."""
    if path.startswith("/") or "\\" in path:
        raise ValueError("package path must be product-relative with forward slashes")
    if any(unicodedata.category(char) == "Cc" for char in path):
        raise ValueError("package path must not contain control characters")
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise ValueError("package path must be normalized, below the product root")
    return path


ProductPath = Annotated[str, Field(max_length=1024), AfterValidator(_product_relative)]


class LocalPackage(Strict):
    """The package the source directory must build: no released tag exists for it."""

    name: Name
    distribution: Distribution
    version: Version


class LocalPackageInstall(Strict):
    """One package source directory at an exact commit of a repository its product owns.

    The `package_source` route is the kit's; no catalog, release tag or binding applies.
    """

    route: Literal["package_source"]
    project_id: uuid.UUID
    repository_id: Annotated[str, Field(min_length=1, max_length=255)]
    commit: SHA
    path: ProductPath
    package: LocalPackage
    core_version: Version
    python_version: Version
    tooling_commit: SHA


#: What a Task's `install` holds: a released catalog closure or a product-local source.
InstallTarget = CatalogInstall | LocalPackageInstall

_INSTALL_TARGET = TypeAdapter(InstallTarget)


def parse_install_target(payload: dict) -> InstallTarget:
    """The typed target of a stored `Task.install`; each route refuses the other's fields."""
    return _INSTALL_TARGET.validate_python(payload)


def preflight_mismatch(result: InstallPreflight, install: InstallTarget) -> str | None:
    """What a kit answer differs in from the saved target, or `None` when it is its result.

    A source answer attests only the package name, the supplied path, the version and the
    product core; the commit, ownership, containment and distribution stay unattested.
    """
    if isinstance(install, CatalogInstall):
        return result.provenance_mismatch(install)
    if result.target is not None and result.target.route != install.route:
        return "target.route"
    if result.package != install.package.name:
        return "package"
    if result.status == "incompatible":
        return None
    if result.target.path != install.path:
        return "target.path"
    if result.target.version != install.package.version:
        return "target.version"
    if result.product_core != install.core_version:
        return "product_core"
    return None


class LocalSourceEvidence(Strict):
    """The saved source directory as the probe read it at the exact commit, as data only.

    `metadata_sha256` is the kit's digest of `package.yaml` and its default binding;
    a package without a default binding has neither `default_binding` nor `binding_sha256`.
    """

    tree: SHA
    entry_point: Annotated[str, Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")]
    metadata_sha256: Digest
    requires_core: Annotated[str, Field(min_length=1, max_length=255)]
    requires_python: Annotated[str, Field(min_length=1, max_length=255)] | None
    default_binding: Annotated[str, Field(min_length=1, max_length=255)] | None
    binding_sha256: Digest | None

    @model_validator(mode="after")
    def binding_pairing(self):
        if (self.default_binding is None) != (self.binding_sha256 is None):
            raise ValueError("a default binding and its digest are declared together")
        return self


class LocalInstalledEvidence(Strict):
    """The wheel `kit add --package-source` retained, read back from the product itself.

    `wheel` is product-relative under `services/backend/packages/`; `files_compared` counts
    the wheel members found byte-identical in the installed backend environment.
    """

    wheel: ProductPath
    wheel_sha256: Digest
    wheel_metadata_sha256: Digest
    lock_sha256: Digest
    direct_url: Annotated[str, Field(pattern=r"^file://", max_length=4096)]
    version: Version
    files_compared: Annotated[int, Field(ge=1)]
    manifest_sha256: Digest
    binding_sha256: Digest | None


class LocalInstallVerification(Strict):
    """The fixed probe's reading of one `LocalPackageInstall`, validated orchestrator-side.

    `target` repeats the saved target the probe was given and claims no ownership;
    `provenance` reads the committed source only, `readback` adds the installed artifact.
    `python_version` is the observed target interpreter the source's `requires-python`
    admitted; `protected_sha256` is the orchestrator's own snapshot at that boundary.
    """

    mode: Literal["provenance", "readback"]
    target: LocalPackageInstall
    head: SHA
    core_version: Version
    python_version: Version
    tooling_commit: SHA
    source: LocalSourceEvidence
    #: Untracked or ignored entries below the source path, never package input.
    residue: list[ProductPath] = Field(max_length=256)
    installed: LocalInstalledEvidence | None
    protected_sha256: dict[str, Digest]

    @model_validator(mode="after")
    def mode_shape(self):
        if (self.mode == "readback") != (self.installed is not None):
            raise ValueError("exactly a readback carries installed evidence")
        if self.mode == "provenance" and self.residue:
            raise ValueError("provenance admits no uncommitted source entry")
        return self


def local_verification_mismatch(
    verification: LocalInstallVerification,
    install: LocalPackageInstall,
    provenance: LocalInstallVerification | None = None,
) -> str | None:
    """The first field a verification differs in from its admitted target, or `None`.

    Order: saved target, exact HEAD, observed core and tooling; a readback then keeps
    its provenance's source and protected bytes, and its wheel, lock, version and binding.
    """
    for key in LocalPackageInstall.model_fields:
        if getattr(verification.target, key) != getattr(install, key):
            return f"target.{key}"
    if verification.head != install.commit:
        return "head"
    for key in ("core_version", "tooling_commit"):
        if getattr(verification, key) != getattr(install, key):
            return key
    if verification.mode == "provenance":
        return None
    if provenance is None or provenance.mode != "provenance":
        return "provenance"
    if verification.source != provenance.source:
        return "source"
    if verification.protected_sha256 != provenance.protected_sha256:
        return "protected_sha256"
    installed = verification.installed
    if installed.wheel_metadata_sha256 != verification.source.metadata_sha256:
        return "installed.wheel_metadata_sha256"
    if installed.lock_sha256 != installed.wheel_sha256:
        return "installed.lock_sha256"
    if installed.version != install.package.version:
        return "installed.version"
    if installed.binding_sha256 != verification.source.binding_sha256:
        return "installed.binding_sha256"
    return None
