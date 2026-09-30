"""Reach the next delivery cycle of an owed owner notification in a service test.

The API decides whether an attempt is due on its own clock, from the record's
``last_attempt_at``. A test that wants the next cycle moves that stamp one
``OWNER_NOTIFICATION_ATTEMPT_INTERVAL`` into the past directly in Postgres — the
one thing it does to the record that the API does not.
"""

from __future__ import annotations

from datetime import datetime
import json
import os

import asyncpg

from shared.contracts.dto.owner_notification import OWNER_NOTIFICATION_ATTEMPT_INTERVAL


async def age_last_attempt_by_one_interval(
    source_id: str, stamp: datetime, *, story_record: bool
) -> None:
    """Move the record's ``last_attempt_at`` from ``stamp`` one interval back."""
    aged = json.dumps((stamp - OWNER_NOTIFICATION_ATTEMPT_INTERVAL).isoformat())
    connection = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        if story_record:
            await connection.execute(
                "UPDATE stories SET owner_notification = jsonb_set("
                "owner_notification::jsonb, '{last_attempt_at}', $2::jsonb)::json "
                "WHERE id = $1",
                source_id,
                aged,
            )
        else:
            await connection.execute(
                "UPDATE runs SET metadata = jsonb_set("
                "metadata::jsonb, '{owner_notification,last_attempt_at}', $2::jsonb)::json "
                "WHERE id = $1",
                source_id,
                aged,
            )
    finally:
        await connection.close()
