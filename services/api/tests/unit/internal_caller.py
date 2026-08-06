"""The credential a router test has to arrive with.

Every route under `/api` is closed by `require_authenticated_caller`, so a test
that drives a handler must come as somebody. These tests stand in for the internal
services that actually call those routes — the scheduler, the consumers, the bot —
so they carry the header those services carry by construction
(`shared/clients/internal_api.py`). Tests about the gate itself do not import this:
they are the ones that must arrive as nobody.
"""

import os

INTERNAL_HEADERS = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
