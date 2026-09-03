"""Reads of the self-hosted Docker registry: which exact images are published.

The deploy path has to answer one question before it spends anything: are the
images of *this* commit in the registry yet. Nothing here writes; publishing is
the project's own CI, and a deploy that cannot find an image refuses rather than
building or retriggering one.
"""

from dataclasses import dataclass
import os
import re

import httpx

from shared.log_config import get_logger

logger = get_logger(__name__)

# `docker/metadata-action@v5` publishes `type=sha` with its documented defaults —
# `prefix=sha-`, `suffix=`, `format=short` — and `short` is the first seven hex
# characters of the commit SHA. The generated project's CI declares `type=sha`
# with no attributes, so this is exactly the tag it pushes. The resolver must
# produce the same string byte for byte: a near miss is a deploy that cannot pull
# at all, and a deploy that pulls something else is the defect this closes.
SHA_TAG_PREFIX = "sha-"
SHORT_SHA_LENGTH = 7

_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

# What a registry may answer with for a manifest. buildx pushes an OCI manifest
# for a single-platform build and an index when it builds several, and an older
# daemon still writes the Docker media types, so all four are accepted rather
# than guessing which one this registry holds.
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class RegistryError(RuntimeError):
    """The registry could not be asked, so nothing is known about the image."""


def sha_image_tag(head_sha: str) -> str:
    """The image tag the project's CI publishes for one commit.

    Derived from the SHA alone, so the deploy names the same tag the build
    named without either side reading the other's state.
    """
    sha = head_sha.strip().lower()
    if not _COMMIT_SHA.fullmatch(sha):
        raise ValueError(f"a SHA image tag needs a full commit SHA, got {head_sha!r}")
    return f"{SHA_TAG_PREFIX}{sha[:SHORT_SHA_LENGTH]}"


@dataclass(frozen=True)
class ImageReference:
    """One `host/repository:tag` reference, split into the parts a registry read needs."""

    registry: str
    repository: str
    tag: str

    def __str__(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag}"


def parse_image_reference(reference: str) -> ImageReference:
    """Split `host/owner/name:tag` into registry, repository and tag.

    A reference without a tag, or without a registry host, is a malformed
    reference rather than a defaulted one: `:latest` by omission is the very
    behaviour this module exists to remove.
    """
    remainder, separator, tag = reference.rpartition(":")
    if not separator or "/" in tag or not remainder:
        raise ValueError(f"image reference names no tag: {reference!r}")
    registry, separator, repository = remainder.partition("/")
    if not separator or not registry or not repository:
        raise ValueError(f"image reference names no registry repository: {reference!r}")
    return ImageReference(registry=registry, repository=repository, tag=tag)


def registry_credentials() -> tuple[str, str, str]:
    """Base URL and basic-auth credentials of the self-hosted registry."""
    host = os.getenv("ORCHESTRATOR_HOSTNAME")
    if not host:
        raise RegistryError("ORCHESTRATOR_HOSTNAME is not set")
    username = os.getenv("REGISTRY_USER")
    if not username:
        raise RegistryError("REGISTRY_USER is not set")
    password = os.getenv("REGISTRY_PASSWORD")
    if not password:
        raise RegistryError("REGISTRY_PASSWORD is not set")
    base = host if host.startswith(("http://", "https://")) else f"https://{host}"
    return base.rstrip("/"), username, password


class DockerRegistryClient:
    """Registry v2 reads against the orchestrator's own registry."""

    def __init__(self, timeout: int = 20):
        self._base, self._username, self._password = registry_credentials()
        self._timeout = timeout

    async def manifest_digest(self, repository: str, tag: str) -> str | None:
        """The digest `repository:tag` resolves to, or None when it does not exist.

        A registry that cannot be reached or refuses the read raises: "not
        published" and "not asked" are different answers, and only the first one
        may ever let a deploy conclude anything.
        """
        url = f"{self._base}/v2/{repository}/manifests/{tag}"
        try:
            async with httpx.AsyncClient(
                auth=(self._username, self._password), timeout=self._timeout
            ) as client:
                response = await client.get(url, headers={"Accept": _MANIFEST_ACCEPT})
        except httpx.HTTPError as error:
            raise RegistryError(
                f"registry could not be read for {repository}:{tag}: {type(error).__name__}"
            ) from error
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.is_error:
            # Only 404 is an answer about the image. Every other status — 401 on
            # rotated credentials, 5xx from the registry — is the read failing,
            # and it has to keep the name it fails under: the deploy routes
            # `RegistryError` to IMAGE_REGISTRY_UNREADABLE, while an
            # `httpx.HTTPStatusError` escaping here would arrive at the generic
            # handler as an untyped "deploy failed".
            raise RegistryError(
                f"registry answered HTTP {response.status_code} for {repository}:{tag}"
            )
        digest = response.headers.get("Docker-Content-Digest")
        if not digest:
            raise RegistryError(f"registry returned no digest for {repository}:{tag}")
        return digest
