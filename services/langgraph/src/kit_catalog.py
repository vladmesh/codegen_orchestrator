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
list could install what the kit no longer releases. A transport failure is retried a few
times first, about `sum(CATALOG_TRANSPORT_BACKOFF_SECONDS)` in all, because the commonest one
is the network of a container the deploy has just restarted; a status or an invalid body is
the source's answer and is not asked again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from functools import lru_cache
import hashlib
import json
import time

from framework.catalog import (
    CATALOG_PATH,
    Catalog,
    CatalogError,
    CatalogLibrary,
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
CATALOG_RESOURCE_MAX_BYTES = 262144
#: The wait before each retry of a catalog fetch that failed in transport.
CATALOG_TRANSPORT_BACKOFF_SECONDS = (1.0, 3.0, 6.0)


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
    libraries: tuple[CatalogLibrary, ...] = ()
    bindings: dict[str, str] = field(default_factory=dict)
    manifests: dict[str, str] = field(default_factory=dict)
    digest: str = ""
    raw: str = ""

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
    return KitCatalog(
        source=source,
        core_version=core_version,
        packages=tuple(packages),
        libraries=catalog.libraries,
        digest=hashlib.sha256(
            json.dumps(
                asdict(catalog), sort_keys=True, default=lambda value: value.model_dump(mode="json")
            ).encode()
        ).hexdigest(),
    )


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        component_source: str | None = None,
    ) -> None:
        self.url = url
        self.component_source = component_source
        self._timeout = timeout
        self._ttl = ttl
        self._clock = clock
        self._sleep = sleep
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

    async def _get_catalog(self) -> httpx.Response | KitCatalogUnavailable:
        """The catalog response, retrying only a request that did not complete.

        A URL no request can be made to (`UnsupportedProtocol`, `InvalidURL`) is a
        configuration error, not a passing one, and answers at once.
        """
        delays = iter(CATALOG_TRANSPORT_BACKOFF_SECONDS)
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    return await client.get(self.url)
            except (httpx.UnsupportedProtocol, httpx.InvalidURL) as error:
                return self._transport_failure(error)
            except httpx.HTTPError as error:
                delay = next(delays, None)
                if delay is None:
                    return self._transport_failure(error)
                logger.info(
                    "kit_catalog_transport_retry",
                    source=self.url,
                    delay=delay,
                    detail=f"{type(error).__name__}: {error}",
                )
                await self._sleep(delay)

    def _transport_failure(self, error: Exception) -> KitCatalogUnavailable:
        return KitCatalogUnavailable(
            self.url, KitCatalogFailure.TRANSPORT, f"{type(error).__name__}: {error}"
        )

    async def _fetch(self) -> KitCatalogAnswer:
        response = await self._get_catalog()
        if isinstance(response, KitCatalogUnavailable):
            return response
        if response.status_code != httpx.codes.OK:
            return KitCatalogUnavailable(
                self.url, KitCatalogFailure.STATUS, f"HTTP {response.status_code}"
            )
        try:
            catalog = parse_catalog(response.text, self.url)
        except CatalogError as error:
            return KitCatalogUnavailable(self.url, KitCatalogFailure.INVALID, str(error))
        answer = installable(catalog, self.url)
        bindings = {}
        manifests = {}
        if self.component_source is not None:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    for item in answer.packages:
                        if item.package.default_binding is None:
                            continue
                        module, resource = item.package.default_binding.split(":", 1)
                        path = f"{item.package.path}/{module.replace('.', '/')}/{resource}"
                        url = f"{self.component_source.rstrip('/')}/{item.version.tag}/{path}"
                        result = await client.get(url)
                        result.raise_for_status()
                        if len(result.content) > CATALOG_RESOURCE_MAX_BYTES:
                            raise ValueError("default binding resource exceeds read limit")
                        bindings[item.name] = result.text
                        manifest_url = (
                            f"{self.component_source.rstrip('/')}/{item.version.tag}/"
                            f"{item.package.path}/{module.replace('.', '/')}/package.yaml"
                        )
                        manifest_response = await client.get(manifest_url)
                        manifest_response.raise_for_status()
                        if len(manifest_response.content) > CATALOG_RESOURCE_MAX_BYTES:
                            raise ValueError("package manifest exceeds read limit")
                        manifests[item.name] = manifest_response.text
            except (httpx.HTTPError, ValueError) as error:
                return KitCatalogUnavailable(
                    self.url,
                    KitCatalogFailure.INVALID,
                    f"binding_unavailable: {type(error).__name__}",
                )
        return replace(answer, bindings=bindings, manifests=manifests, raw=response.text)


@lru_cache
def get_kit_catalog_reader() -> KitCatalogReader:
    """The process's one reader, at the configured source and ref."""
    settings = get_settings()
    return KitCatalogReader(
        catalog_url(settings.kit_catalog_source, settings.kit_catalog_ref),
        component_source=settings.kit_catalog_source,
    )
