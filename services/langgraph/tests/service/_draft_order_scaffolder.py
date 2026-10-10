"""The scaffolder's own entrypoint on one full-scaffold entry, GitHub refusing the repository.

GitHub is the one controlled edge: creating the repository answers a server error, as a
GitHub outage does. Everything after it is the scaffolder's own failure handling.
"""

import asyncio
import json
import os

import httpx

from shared.queues import SCAFFOLD_QUEUE
from shared.redis import RedisStreamClient
from src import consumer


class _GitHubRefusing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_org_token(self, org):
        return "controlled-token"

    async def create_repo(self, org, name, *, private):
        request = httpx.Request("POST", f"https://api.github.com/orgs/{org}/repos")
        raise httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(502, request=request)
        )


async def run():
    stream = RedisStreamClient()
    await stream.connect()
    consumer.GitHubAppClient = _GitHubRefusing
    try:
        entry = os.environ["SCAFFOLD_ENTRY"]
        [(_, fields)] = await stream.redis.xrange(SCAFFOLD_QUEUE, min=entry, max=entry)
        result = await consumer.process_scaffold_job(json.loads(fields["data"]), stream)
        print(json.dumps(result))
    finally:
        await stream.close()


asyncio.run(run())
