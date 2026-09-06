"""Package protocol v1 public API and activation runtime."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib import metadata
from pathlib import Path
import re
from typing import Any, Protocol, runtime_checkable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
import yaml

PACKAGE_PROTOCOL_VERSION = 1
CORE_VERSION = "1.2.0"
ENTRY_POINT_GROUP = "codegen_kit.packages"


class PackageActivationError(RuntimeError):
    """Base class for a named package startup failure."""


class PackageManifestError(PackageActivationError):
    """An installed package carries an invalid package.yaml."""


class UnknownPackageManifestFieldError(PackageManifestError):
    """The package manifest contains an unknown field."""


class MissingPackageIdentityError(PackageManifestError):
    """The package manifest omits its name or version."""


class MalformedPackagePrefixError(PackageManifestError):
    """The package HTTP prefix is malformed."""


class UnimplementedDeploymentModeError(PackageManifestError):
    """The package declares a delivery form this core does not implement."""


class InvalidPackageModuleRootError(PackageActivationError):
    """The entry-point module does not resolve to a package directory."""


class InstalledPackageNotListedError(PackageActivationError):
    """An installed package was not allowlisted by the product."""


class ListedPackageNotInstalledError(PackageActivationError):
    """An allowlisted package has no installed entry point."""


class IncompatiblePackageProtocolError(PackageActivationError):
    """A package declares a protocol version unsupported by this core."""


class IncompatibleCoreVersionError(PackageActivationError):
    """A package's core version constraint excludes this core."""


class DuplicatePackageHttpPrefixError(PackageActivationError):
    """Two activated packages declare the same HTTP mount prefix."""


@dataclass(frozen=True)
class PackageManifest:
    """The activation subset of a validated package.yaml."""

    protocol_version: int
    name: str
    version: str
    requires_core: str
    http_prefix: str
    deployment_modes: tuple[str, ...] = ("in_process",)
    database_schema: str | None = None
    database_migrations: str | None = None


@runtime_checkable
class Package(Protocol):
    """Runtime object loaded from a ``codegen_kit.packages`` entry point."""

    router: Any

    def startup(self, application: Any) -> Awaitable[None]:
        """Start package resources after core resources are connected."""

    def shutdown(self, application: Any) -> Awaitable[None]:
        """Stop package resources before core resources are disconnected."""


@dataclass(frozen=True)
class ActivatedPackage:
    """One validated installed package and its loaded runtime object."""

    manifest: PackageManifest
    runtime: Package
    package_root: Path
    manifest_sha256: str


def _package_root(entry_point: metadata.EntryPoint) -> tuple[Path, Path]:
    """Resolve the entry-point module to its required package directory."""

    distribution = entry_point.dist
    if distribution is None:
        raise PackageManifestError(f"entry point {entry_point.name!r} has no distribution")
    module_name = entry_point.value.partition(":")[0]
    module_path = Path(*module_name.split("."))
    package_root = Path(distribution.locate_file(module_path))
    if not module_name or not package_root.is_dir():
        raise InvalidPackageModuleRootError(
            f"entry point {entry_point.name!r} module {module_name!r} "
            "must resolve to an installed package directory"
        )
    return package_root, module_path


def _manifest_path(entry_point: metadata.EntryPoint, package_root: Path) -> Path:
    """Resolve the sole manifest at its protocol-defined package location."""

    distribution = entry_point.dist
    if distribution is None:
        raise PackageManifestError(f"entry point {entry_point.name!r} has no distribution")
    candidates = [file for file in distribution.files or () if file.name == "package.yaml"]
    if len(candidates) != 1:
        raise PackageManifestError(
            f"package {entry_point.name!r} must install exactly one package.yaml"
        )
    manifest_path = Path(distribution.locate_file(candidates[0]))
    expected_path = package_root / "package.yaml"
    if manifest_path != expected_path:
        raise PackageManifestError(
            f"package {entry_point.name!r} must install package.yaml at {expected_path}"
        )
    return manifest_path


def _load_manifest_data(path: Path) -> dict[str, Any]:
    """Read a manifest as a mapping before applying protocol validation."""

    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise PackageManifestError(f"cannot read {path}: {error}") from error
    if not isinstance(data, dict):
        raise PackageManifestError("package.yaml must contain an object")
    return data


def _validate_manifest_fields(data: dict[str, Any]) -> None:
    """Reject unknown and missing activation fields with stable errors."""

    known = {
        "protocol_version",
        "name",
        "version",
        "requires_core",
        "provides",
        "requires",
        "package_dependencies",
        "http",
        "database",
        "events",
        "settings_schema",
        "jobs_schema",
        "deployment",
        "environment",
        "resources",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise UnknownPackageManifestFieldError(
            f"package.yaml contains unknown field {unknown[0]!r}"
        )
    for field in ("name", "version"):
        if not data.get(field):
            raise MissingPackageIdentityError(
                f"package.yaml is missing required identity field {field!r}"
            )
    missing = [field for field in ("protocol_version", "requires_core") if field not in data]
    if missing:
        raise PackageManifestError(f"package.yaml is missing required field {missing[0]!r}")


def _http_prefix(data: dict[str, Any]) -> str:
    """Return a validated absolute package route prefix."""

    http = data.get("http")
    prefix = http.get("prefix") if isinstance(http, dict) else None
    if (
        not isinstance(prefix, str)
        or not prefix.startswith("/")
        or prefix == "/"
        or prefix.endswith("/")
        or "//" in prefix
        or "{" in prefix
        or "}" in prefix
    ):
        raise MalformedPackagePrefixError("package.yaml has malformed HTTP prefix")
    return prefix


def _valid_database_schema(schema: object) -> bool:
    return (
        isinstance(schema, str)
        and schema != "public"
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is not None
    )


def _valid_migration_resource(declaration: object) -> bool:
    if not isinstance(declaration, str):
        return False
    module, separator, relative = declaration.partition(":")
    return bool(separator and module and relative and ".." not in Path(relative).parts)


def _database_declaration(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return a fail-closed package schema and migration resource pair."""

    database = data.get("database")
    if database is None:
        return None, None
    if not isinstance(database, dict):
        raise PackageManifestError("package.yaml has malformed database declaration")
    schema = database.get("schema")
    migrations = database.get("migrations")
    if not _valid_database_schema(schema) or not _valid_migration_resource(migrations):
        raise PackageManifestError("package.yaml has malformed database declaration")
    assert isinstance(schema, str) and isinstance(migrations, str)
    return schema, migrations


def _deployment_modes(data: dict[str, Any]) -> tuple[str, ...]:
    """Return the validated additive package deployability declaration."""

    deployment = data.get("deployment", {"modes": ["in_process"]})
    if not isinstance(deployment, dict) or set(deployment) != {"modes"}:
        raise PackageManifestError("package.yaml has malformed deployment declaration")
    modes = deployment["modes"]
    allowed = {"in_process", "container"}
    if (
        not isinstance(modes, list)
        or not modes
        or any(not isinstance(mode, str) or mode not in allowed for mode in modes)
        or len(set(modes)) != len(modes)
    ):
        raise PackageManifestError("package.yaml has malformed deployment declaration")
    if "container" in modes:
        raise UnimplementedDeploymentModeError(
            "package.yaml declares deployment mode 'container', which is refused until this "
            "core implements container delivery"
        )
    return tuple(modes)


def _parse_manifest(path: Path) -> PackageManifest:
    data = _load_manifest_data(path)
    _validate_manifest_fields(data)
    database_schema, database_migrations = _database_declaration(data)
    return PackageManifest(
        protocol_version=data["protocol_version"],
        name=data["name"],
        version=data["version"],
        requires_core=data["requires_core"],
        http_prefix=_http_prefix(data),
        deployment_modes=_deployment_modes(data),
        database_schema=database_schema,
        database_migrations=database_migrations,
    )


def _entry_points() -> Sequence[metadata.EntryPoint]:
    return tuple(metadata.entry_points(group=ENTRY_POINT_GROUP))


def _validate_allowlist(listed: set[str], discovered: dict[str, metadata.EntryPoint]) -> None:
    """Require installed entry points and the product allowlist to agree."""

    not_listed = sorted(set(discovered) - listed)
    if not_listed:
        raise InstalledPackageNotListedError(
            f"installed package {not_listed[0]!r} is not listed in the product manifest"
        )
    not_installed = sorted(listed - set(discovered))
    if not_installed:
        raise ListedPackageNotInstalledError(
            f"listed package {not_installed[0]!r} is not installed"
        )


def _validate_compatibility(name: str, manifest: PackageManifest) -> None:
    """Validate package protocol and core-version compatibility."""

    if manifest.protocol_version != PACKAGE_PROTOCOL_VERSION:
        raise IncompatiblePackageProtocolError(
            f"package {name!r} requires protocol {manifest.protocol_version}; "
            f"core provides {PACKAGE_PROTOCOL_VERSION}"
        )
    try:
        compatible = Version(CORE_VERSION) in SpecifierSet(manifest.requires_core)
    except (InvalidSpecifier, InvalidVersion, TypeError) as error:
        raise IncompatibleCoreVersionError(
            f"package {name!r} has invalid core requirement {manifest.requires_core!r}"
        ) from error
    if not compatible:
        raise IncompatibleCoreVersionError(
            f"package {name!r} requires core {manifest.requires_core}; core is {CORE_VERSION}"
        )


def _activate_entry_point(name: str, entry_point: metadata.EntryPoint) -> ActivatedPackage:
    """Validate and load one allowlisted entry point."""

    package_root, _ = _package_root(entry_point)
    manifest_path = _manifest_path(entry_point, package_root)
    manifest = _parse_manifest(manifest_path)
    if manifest.name != name:
        raise PackageManifestError(
            f"entry point {name!r} does not match package manifest name {manifest.name!r}"
        )
    _validate_compatibility(name, manifest)
    if entry_point.dist is not None and manifest.version != entry_point.dist.version:
        raise PackageManifestError(
            f"package {name!r} manifest version {manifest.version!r} does not match "
            f"distribution version {entry_point.dist.version!r}"
        )
    runtime = entry_point.load()
    if not isinstance(runtime, Package):
        raise PackageActivationError(
            f"package {name!r} entry point does not implement the Package protocol"
        )
    return ActivatedPackage(
        manifest=manifest,
        runtime=runtime,
        package_root=package_root,
        manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
    )


def _validate_http_prefixes(packages: Sequence[ActivatedPackage]) -> None:
    """Refuse ambiguous package router mounts before mutating the application."""

    owners: dict[str, str] = {}
    for package in packages:
        prefix = package.manifest.http_prefix
        previous = owners.get(prefix)
        if previous is not None:
            raise DuplicatePackageHttpPrefixError(
                f"packages {previous!r} and {package.manifest.name!r} "
                f"declare duplicate HTTP prefix {prefix!r}"
            )
        owners[prefix] = package.manifest.name


def discover_packages(
    listed: Sequence[str],
    *,
    entry_points: Callable[[], Sequence[metadata.EntryPoint]] = _entry_points,
) -> list[ActivatedPackage]:
    """Resolve and validate the product's complete package allowlist."""

    listed_set = set(listed)
    discovered = {entry_point.name: entry_point for entry_point in entry_points()}
    _validate_allowlist(listed_set, discovered)
    activated = [_activate_entry_point(name, discovered[name]) for name in listed]
    _validate_http_prefixes(activated)
    return activated


def configure_packages(application: Any, listed: Sequence[str]) -> None:
    """Validate packages and mount their routers on an application."""

    activated = discover_packages(listed)
    application.state.codegen_packages = activated
    for package in activated:
        application.include_router(package.runtime.router, prefix=package.manifest.http_prefix)


def generated_packages(manifest_path: Path | None = None) -> list[str]:
    """Return the generated active set after refusing a stale product manifest."""

    from ._active_packages import ACTIVE_PACKAGES

    generated = [identity["name"] for identity in ACTIVE_PACKAGES]
    listed = product_packages(manifest_path)
    if generated != listed:
        raise PackageActivationError(
            "generated package contract is stale; run make generate-from-spec"
        )
    return generated


def discover_generated_packages(
    manifest_path: Path | None = None,
) -> list[ActivatedPackage]:
    """Resolve the generated set and refuse a wheel changed since generation."""

    from ._active_packages import ACTIVE_PACKAGES

    activated = discover_packages(generated_packages(manifest_path))
    expected = {item["name"]: item for item in ACTIVE_PACKAGES}
    for package in activated:
        identity = expected[package.manifest.name]
        if package.manifest.version != identity["version"]:
            raise PackageActivationError(
                f"package {package.manifest.name!r} changed from generated version "
                f"{identity['version']!r} to installed version {package.manifest.version!r}; "
                "run make generate-from-spec"
            )
        if package.manifest_sha256 != identity["manifest_sha256"]:
            raise PackageActivationError(
                f"package {package.manifest.name!r} manifest changed since generation; "
                "run make generate-from-spec"
            )
    return activated


def configure_generated_packages(application: Any) -> None:
    """Mount exactly the packages whose contracts were generated into the product."""

    activated = discover_generated_packages()
    application.state.codegen_packages = activated
    for package in activated:
        application.include_router(package.runtime.router, prefix=package.manifest.http_prefix)


async def startup_packages(application: Any) -> None:
    """Start packages in product-manifest order."""

    started: list[ActivatedPackage] = []
    application.state.codegen_started_packages = started
    for package in application.state.codegen_packages:
        await package.runtime.startup(application)
        started.append(package)


async def shutdown_packages(application: Any, *, suppress_errors: bool = False) -> None:
    """Stop successfully started packages in reverse order, attempting every stop."""

    first_error: Exception | None = None
    cancellation: CancelledError | None = None
    started = getattr(application.state, "codegen_started_packages", [])
    while started:
        package = started.pop()
        try:
            await package.runtime.shutdown(application)
        except CancelledError as error:
            if cancellation is None:
                cancellation = error
        except Exception as error:
            if first_error is None:
                first_error = error
    if cancellation is not None:
        raise cancellation
    if first_error is not None and not suppress_errors:
        raise first_error


def product_packages(manifest_path: Path | None = None) -> list[str]:
    """Read the explicit package allowlist from the backend service manifest."""

    path = (
        manifest_path or Path(__file__).resolve().parent.parent / "services/backend/manifest.yaml"
    )
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise PackageActivationError(f"cannot read product manifest {path}: {error}") from error
    packages = data.get("packages", []) if isinstance(data, dict) else []
    if not isinstance(packages, list) or any(not isinstance(name, str) for name in packages):
        raise PackageActivationError("product manifest packages must be a list of names")
    return packages
