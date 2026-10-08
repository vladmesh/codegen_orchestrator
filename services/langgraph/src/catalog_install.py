"""One catalog selection, one typed install task; no prose classification."""

import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import tempfile

from framework.bindings import Binding, BindingError, ParsedCreate, load_binding, validate_binding
from framework.bindings_v2 import BindingV2
from framework.catalog import Catalog, CatalogError
from framework.spec.packages import PackageManifest
import yaml

from shared.contracts.dto.catalog_install import CatalogInstall, DefaultBinding, InstallComponent

from .kit_catalog import KitCatalog, KitCatalogAnswer


class InstallRefusal(ValueError):
    pass


def load_catalog_binding(content: str) -> Binding | BindingV2:
    """Use the pinned kit's data-only version dispatch on snapshot bytes."""
    with tempfile.TemporaryDirectory(prefix="catalog-binding-") as scratch:
        path = Path(scratch) / "default.yaml"
        path.write_text(content)
        try:
            return load_binding(path)
        except BindingError as error:
            raise BindingError(str(error).replace(str(path), "catalog snapshot")) from error


def plan_install_payload(
    snapshot: KitCatalogAnswer, name: str, python_version: str
) -> CatalogInstall:
    if not isinstance(snapshot, KitCatalog):
        raise InstallRefusal("catalog_unavailable: no install task can be created")
    selected = next((item for item in snapshot.packages if item.name == name), None)
    if selected is None:
        raise InstallRefusal(f"unknown_package: {name} is absent or incompatible with this core")
    package = selected.package
    if not package.default_binding or name not in snapshot.bindings:
        raise InstallRefusal("binding_unavailable: the selected release has no default resource")
    if package.extends is not None:
        raise InstallRefusal("extension_requires_parent: select an ordinary catalog package")
    libraries = []
    for recommendation in package.recommended_with:
        library = next(
            (item for item in snapshot.libraries if item.name == recommendation.library), None
        )
        if library is None:
            raise InstallRefusal(f"recommendation_missing: {recommendation.library}")
        try:
            version = library.select(python_version)
        except CatalogError as error:
            raise InstallRefusal(f"incompatible_library: {error}") from error
        libraries.append(
            InstallComponent(
                name=library.name,
                distribution=library.distribution,
                version=version.version,
                tag=version.tag,
            )
        )
    content = snapshot.bindings[name]
    try:
        binding = load_catalog_binding(content)
        if name not in snapshot.manifests:
            raise InstallRefusal("binding_manifest_unavailable")
        manifest = PackageManifest.model_validate(yaml.safe_load(snapshot.manifests[name]))
        if (
            manifest.name != name
            or manifest.version != selected.version.version
            or manifest.default_binding != package.default_binding
        ):
            raise InstallRefusal("binding_manifest_identity_mismatch")
    except ValueError as error:
        raise InstallRefusal(f"invalid_binding: {error}") from error
    functions = sorted(
        {item.parse.function for item in binding.commands if isinstance(item, ParsedCreate)}
    )
    for function in functions:
        owner, symbol = function.split(".", 1)
        library = next((item for item in snapshot.libraries if item.name == owner), None)
        if (
            owner not in {item.name for item in libraries}
            or library is None
            or symbol not in {item.name for item in library.functions}
        ):
            raise InstallRefusal(f"binding_dependency_missing: {function}")
    try:
        validate_binding(
            binding,
            manifest,
            Catalog(
                format_version=1,
                packages=tuple(item.package for item in snapshot.packages),
                libraries=snapshot.libraries,
            ),
        )
    except ValueError as error:
        raise InstallRefusal(f"invalid_binding_contract: {error}") from error
    return CatalogInstall(
        package=InstallComponent(
            name=name,
            distribution=package.distribution,
            version=selected.version.version,
            tag=selected.version.tag,
        ),
        libraries=libraries,
        binding=DefaultBinding(
            package=binding.package,
            resource=package.default_binding,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            functions=functions,
        ),
        core_version=snapshot.core_version,
        python_version=python_version,
        catalog_digest=snapshot.digest,
        tooling_commit=json.loads(distribution("codegen-kit-tooling").read_text("direct_url.json"))[
            "vcs_info"
        ]["commit_id"],
    )
