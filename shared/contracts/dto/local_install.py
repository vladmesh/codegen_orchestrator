"""A product-local kit package INSTALL target, stored but not executable yet."""

from typing import Annotated, Literal
import unicodedata
import uuid

from pydantic import AfterValidator, Field, TypeAdapter

from shared.contracts.dto.catalog_install import (
    SHA,
    CatalogInstall,
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
