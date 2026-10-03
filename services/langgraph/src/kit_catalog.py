"""The kit's live package catalog, as the Architect plans packages from it.

The kit lists every released package in `packages/catalog.yaml` on its default branch,
and `kit add <name>` installs from that file live. The Architect reads the same file at
planning time, so a package release is plannable the moment the kit publishes it, with no
orchestrator change. This module is the only reader: it fetches the file over HTTP with a
bounded timeout, parses it with the kit's own loader (`framework.catalog.parse_catalog`),
and keeps only the packages with a released version that admits the kit core the
orchestrator pins (`framework.spec.package_resolution.CORE_VERSION`).

Any failure to read or parse is a typed `KitCatalogUnavailable` answer, never an exception
into planning and never a stale or hard-coded list: a planner told that the catalog is
unavailable plans no package this time, which is a correct plan, while one handed an old
list could install what the kit no longer releases.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
import time

from framework.catalog import (
    CATALOG_PATH,
    Catalog,
    CatalogError,
    CatalogPackage,
    CatalogVersion,
    IncompatibleCatalogVersionError,
    parse_catalog,
)
from framework.spec.package_resolution import CORE_VERSION
import httpx
import structlog

from .config.settings import get_settings

logger = structlog.get_logger(__name__)

#: One fetch never holds a planning run longer than this.
CATALOG_TIMEOUT_SECONDS = 10.0
#: A read this recent answers again without a fetch; a release waits at most this long.
CATALOG_CACHE_SECONDS = 300.0


class KitCatalogFailure(StrEnum):
    """Why the catalog could not be read this time."""

    #: The request did not complete: connection, DNS, TLS or the timeout.
    TRANSPORT = "transport"
    #: The source answered with a status other than 200.
    STATUS = "status"
    #: The body is not a catalog the kit's loader accepts: YAML or validation.
    INVALID = "invalid"


@dataclass(frozen=True)
class InstallablePackage:
    """One catalog package, with the version `kit add` would install for the pinned core."""

    package: CatalogPackage
    version: CatalogVersion

    @property
    def name(self) -> str:
        return self.package.name


@dataclass(frozen=True)
class KitCatalog:
    """The catalog as read from `source`, down to what the pinned core can install."""

    source: str
    core_version: str
    packages: tuple[InstallablePackage, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(package.name for package in self.packages)


@dataclass(frozen=True)
class KitCatalogUnavailable:
    """The catalog could not be read from `source`; no package can be planned."""

    source: str
    failure: KitCatalogFailure
    detail: str


KitCatalogAnswer = KitCatalog | KitCatalogUnavailable


def catalog_url(source: str, ref: str) -> str:
    """The raw catalog file at `ref` of the kit repository whose raw-file base is `source`."""
    return f"{source.rstrip('/')}/{ref}/{CATALOG_PATH}"


def installable(catalog: Catalog, source: str, core_version: str = CORE_VERSION) -> KitCatalog:
    """Keep the packages with a released version that admits `core_version`.

    The version is the one `kit add` picks, `CatalogPackage.select`: the newest whose
    `requires_core` admits the core. A package none of whose versions does is left out,
    because `kit add` would refuse it.
    """
    packages = []
    for package in catalog.packages:
        try:
            version = package.select(core_version)
        except IncompatibleCatalogVersionError:
            logger.info("kit_catalog_package_incompatible", package=package.name, core=core_version)
            continue
        packages.append(InstallablePackage(package=package, version=version))
    return KitCatalog(source=source, core_version=core_version, packages=tuple(packages))


class KitCatalogReader:
    """Reads the catalog from one URL, answering again from a read younger than `ttl`.

    Only a successful read is kept. A failure is answered as it happened and the next
    read tries again; once the kept read is older than `ttl` a failing fetch answers
    unavailable rather than the old catalog.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = CATALOG_TIMEOUT_SECONDS,
        ttl: float = CATALOG_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self._timeout = timeout
        self._ttl = ttl
        self._clock = clock
        self._kept: tuple[float, KitCatalog] | None = None

    async def read(self) -> KitCatalogAnswer:
        if self._kept is not None:
            read_at, catalog = self._kept
            if self._clock() - read_at < self._ttl:
                return catalog
        answer = await self._fetch()
        if isinstance(answer, KitCatalog):
            self._kept = (self._clock(), answer)
            logger.info("kit_catalog_read", source=self.url, packages=sorted(answer.names))
        else:
            logger.warning(
                "kit_catalog_unavailable",
                source=self.url,
                failure=answer.failure.value,
                detail=answer.detail,
            )
        return answer

    async def _fetch(self) -> KitCatalogAnswer:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self.url)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            return KitCatalogUnavailable(
                self.url, KitCatalogFailure.TRANSPORT, f"{type(error).__name__}: {error}"
            )
        if response.status_code != httpx.codes.OK:
            return KitCatalogUnavailable(
                self.url, KitCatalogFailure.STATUS, f"HTTP {response.status_code}"
            )
        try:
            catalog = parse_catalog(response.text, self.url)
        except CatalogError as error:
            return KitCatalogUnavailable(self.url, KitCatalogFailure.INVALID, str(error))
        return installable(catalog, self.url)


@lru_cache
def get_kit_catalog_reader() -> KitCatalogReader:
    """The process's one reader, at the configured source and ref."""
    settings = get_settings()
    return KitCatalogReader(catalog_url(settings.kit_catalog_source, settings.kit_catalog_ref))
