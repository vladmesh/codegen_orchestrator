"""The scaffolder's own entrypoint on one install entry, delivered twice.

Two edges are controlled. GitHub only answers the org token. The native executor
`src.install.run_install` is replaced by one that sends, through the consumer's own fence,
the commands the real executor sends for a mechanical product-local install: the preflight
with its provenance, the commit head, the readback and the publication.
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from shared.contracts.dto.install_operation import InstallCommand
from shared.queues import SCAFFOLD_QUEUE
from shared.redis import RedisStreamClient
from shared.tests.unit.test_local_verification import verification
from src import consumer, install

HEAD = "9" * 40


class _GitHubToken:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_org_token(self, org):
        return "controlled-token"


def _preflight(target) -> dict:
    """The kit's mechanical `check-install --package-source` answer for `target`."""
    return {
        "result_version": 1,
        "package": target.package.name,
        "status": "mechanical",
        "product_core": target.core_version,
        "target": {
            "route": "package_source",
            "path": target.path,
            "version": target.package.version,
            "requires_core": ">=2,<3",
            "metadata_sha256": "4" * 64,
        },
        "glue": [],
        "incompatible": None,
    }


async def run():
    executed = []
    answers = []

    async def controlled_install(context, settings, git_url, token, fence):
        target = context.install
        executed.append(
            {
                "project_id": context.project_id,
                "repository_id": context.repository_id,
                "story_id": context.story_id,
                "operation_id": context.operation_id,
                "install": target.model_dump(mode="json"),
                "git_url": git_url,
            }
        )
        checkout = f"{context.repository_id}/{context.operation_id}"
        proof = target.model_dump(mode="json")
        for body in (
            {
                "stage": "preflight",
                "checkout": checkout,
                "base_sha": target.commit,
                "preflight": _preflight(target),
                "verification": verification(target=proof),
            },
            {"stage": "commit", "head_sha": HEAD},
            {"stage": "readback", "verification": verification("readback", target=proof)},
        ):
            await fence(InstallCommand.model_validate({"action": "checkpoint", **body}))
            answers.append(body["stage"])
        await fence(InstallCommand(action="publish", head_sha=HEAD))
        answers.append("publish")
        return SimpleNamespace(head_sha=HEAD, stages=[], checkout=checkout, checkout_removed=True)

    stream = RedisStreamClient()
    await stream.connect()
    consumer.GitHubAppClient = _GitHubToken
    install.run_install = controlled_install
    try:
        entry = os.environ["SCAFFOLD_ENTRY"]
        [(_, fields)] = await stream.redis.xrange(SCAFFOLD_QUEUE, min=entry, max=entry)
        results = [
            await consumer.process_scaffold_job(json.loads(fields["data"]), stream)
            for _ in range(2)
        ]
        # Service loggers write to stdout; the result goes to its own file.
        Path(os.environ["RESULT_FILE"]).write_text(
            json.dumps({"results": results, "executed": executed, "fenced": answers})
        )
    finally:
        await stream.close()


asyncio.run(run())
