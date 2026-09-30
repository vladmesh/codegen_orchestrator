"""Trusted stand transport: resolve the PO key in the released service container."""

import asyncio
import json
import os
import sys

from redis.asyncio import Redis

from shared.contracts.queues.po import is_po_stream, protect_po_payload, unprotect_po_payload


async def execute(redis, args: list[str]):
    command, stream, *tail = args
    if not is_po_stream(stream):
        raise ValueError
    if command in ("XRANGE", "XREVRANGE"):
        entries = await redis.execute_command(command, stream, *tail)
        return [
            [
                entry_id,
                [item for pair in unprotect_po_payload(stream, fields).items() for item in pair],
            ]
            for entry_id, fields in entries
        ]
    if command == "XADD" and tail[0] == "*":
        fields = dict(zip(tail[1::2], tail[2::2], strict=True))
        return await redis.xadd(stream, protect_po_payload(stream, fields))
    raise ValueError


async def _main() -> int:
    try:
        async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
            result = await execute(redis, sys.argv[1:])
        # Machine output to a trusted test process, not a diagnostic or retained artifact.
        sys.stdout.write(json.dumps(result))
        return 0
    except Exception:
        sys.stderr.write("PO stand transport refused the command or protected payload\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
