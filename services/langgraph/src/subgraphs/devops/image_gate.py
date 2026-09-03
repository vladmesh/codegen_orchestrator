"""Wait, bounded and fail-closed, for this commit's images to be published.

The deploy dispatches within seconds of the merge, while the project's own CI is
still building that commit. Nothing in the deploy path used to look: the workflow
pulled a mutable tag and the run reported the merged SHA over whatever bytes the
registry happened to hold. This gate is the missing read. It waits for the exact
tags the resolver named and, when they are not all there inside the bound,
refuses — it never builds, retriggers or repairs the project's CI, which is the
project's own business, and it never lets a deploy proceed on a hopeful guess.

Its shape is the stand's own worker-release gate (`.github/workflows/stand-e2e.yml`):
validate the exact SHA's publication with the consumer's own contract before
anything billable happens, and treat "no marker" as no work rather than as a
reason to try anyway.
"""

import asyncio

import structlog

from shared.clients.registry import DockerRegistryClient, parse_image_reference

logger = structlog.get_logger()

# How long the deploy waits for the merged commit's images. The project's CI
# starts at the merge and has to run tests and then build and push one image per
# service, so the bound has to cover a cold multi-service buildx run — minutes,
# not seconds. It also has to stay well inside the deploy lock (1 h) that
# serialises deploys of one project, together with the deploy run itself (600 s)
# and its one rerun (600 s): 900 + 600 + 600 is comfortably under that, so a
# gate that waits its full bound can still refuse under its own lock instead of
# having it expire underneath.
IMAGE_PUBLICATION_TIMEOUT_SECONDS = 900
IMAGE_PUBLICATION_POLL_SECONDS = 10


class ImagesNotPublishedError(RuntimeError):
    """The exact commit's images were not in the registry inside the bound."""


def image_references(values: dict[str, str]) -> dict[str, str]:
    """The `*_IMAGE` references one deploy resolved, keyed by contract variable.

    Read back from the resolved environment rather than recomputed, so the gate
    and the evidence can only ever speak about the references the deploy actually
    writes into the target's `.env`.
    """
    return {key: value for key, value in values.items() if key.endswith("_IMAGE") and value}


async def wait_for_published_images(
    references: dict[str, str],
    *,
    timeout_seconds: int = IMAGE_PUBLICATION_TIMEOUT_SECONDS,
    poll_seconds: int = IMAGE_PUBLICATION_POLL_SECONDS,
    registry: DockerRegistryClient | None = None,
) -> dict[str, str]:
    """Return each reference's digest once every one of them exists.

    Raises:
        ImagesNotPublishedError: at least one reference is still absent when the
            bound runs out. A registry that cannot be read at all propagates its
            own error instead: not asked is not the same answer as not there.
    """
    if not references:
        raise ImagesNotPublishedError("this deploy resolved no image references to wait for")

    client = registry or DockerRegistryClient()
    parsed = {key: parse_image_reference(value) for key, value in references.items()}
    digests: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while True:
        for key, reference in parsed.items():
            if key in digests:
                continue
            digest = await client.manifest_digest(reference.repository, reference.tag)
            if digest:
                digests[key] = digest
        missing = [str(parsed[key]) for key in parsed if key not in digests]
        if not missing:
            logger.info("deploy_images_published", images=sorted(digests))
            return digests
        if loop.time() >= deadline:
            raise ImagesNotPublishedError(
                f"the project's CI did not publish {', '.join(sorted(missing))} "
                f"within {timeout_seconds}s"
            )
        logger.info(
            "deploy_images_awaiting_publication",
            missing=sorted(missing),
            timeout_seconds=timeout_seconds,
        )
        await asyncio.sleep(poll_seconds)
