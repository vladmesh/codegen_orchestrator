"""The API's side of the owner-notification attempt claim, for in-memory fakes.

The real claim is a locked read-check-stamp at the API (proved against Postgres
in the service tests). Fakes that store records in memory answer it here the
same way — through the DTO's own ``attempt_due`` and ``supersedes`` — so a test
exercises the scheduler's claim handling against the rule, not a mock that
grants everything. ``ClaimClock`` is the API's clock: a test that wants a second
attempt on the same record moves it forward by the interval.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_INTERVAL,
    OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationAttemptClaim,
)


class ClaimClock:
    """What the API's clock reads when it decides a claim."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def elapse(self, interval: timedelta = OWNER_NOTIFICATION_ATTEMPT_INTERVAL) -> None:
        self.now += interval


def claim(
    clock: ClaimClock,
    read: Callable[[], dict | OwnerNotification | None],
    write: Callable[[dict], None],
) -> OwnerNotificationAttemptClaim:
    """Grant and stamp one attempt when the stored record is owed and due."""
    stored = read()
    if stored is None:
        return OwnerNotificationAttemptClaim(granted=False, notification=None)
    record = OwnerNotification.model_validate(stored)
    if not record.attempt_due(clock.now):
        return OwnerNotificationAttemptClaim(granted=False, notification=record)
    stamped = record.model_copy(update={"last_attempt_at": clock.now})
    write(stamped.model_dump(mode="json"))
    return OwnerNotificationAttemptClaim(granted=True, notification=stamped)


def refuse_superseded(stored: dict | OwnerNotification | None, incoming: dict) -> None:
    """Answer a stale write the way the API does: a 409 naming the newer attempt."""
    if stored is None:
        return
    if OwnerNotification.model_validate(stored).supersedes(
        OwnerNotification.model_validate(incoming)
    ):
        request = httpx.Request("PATCH", "http://api/owner-notification")
        response = httpx.Response(
            httpx.codes.CONFLICT,
            json={"detail": {"code": OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED}},
            request=request,
        )
        raise httpx.HTTPStatusError("superseded", request=request, response=response)


class ClaimsFromWrites:
    """Answer claims on an ``AsyncMock`` API client from what was written through it.

    Routing tests drive the scheduler against a bare mock with no record store.
    The API's record is then the last one the scheduler wrote for that source —
    read back from the mock's own call history, so a test that later replaces a
    write's ``side_effect`` does not lose it — or, before any write, the record
    the test seeded: ``stories``/``runs`` by id, and for a story the mock's
    ``get_story_owner_notification`` answer, which is how those tests hand the
    completion transaction's record to the scheduler.
    """

    def __init__(self, client, clock: ClaimClock | None = None) -> None:
        self.client = client
        self.clock = clock or ClaimClock()
        self.runs: dict[str, dict | OwnerNotification] = {}
        self.stories: dict[str, dict | OwnerNotification] = {}
        # Claim stamps, with how many writes of that kind had happened when they
        # were made: a later write carries the stamp itself and wins.
        self._stamps: dict[tuple[str, str], tuple[int, dict]] = {}
        client.claim_run_owner_notification_attempt.side_effect = self._claim_run
        client.claim_story_owner_notification_attempt.side_effect = self._claim_story

    def _run_writes(self, run_id: str) -> list[dict]:
        written = []
        for call in self.client.update_run.await_args_list:
            source, data = (*call.args, *call.kwargs.values())[:2]
            metadata = data.get("run_metadata") or {}
            if source == run_id and OWNER_NOTIFICATION_KEY in metadata:
                written.append(metadata[OWNER_NOTIFICATION_KEY])
        return written

    def _story_writes(self, story_id: str) -> list[dict]:
        written = []
        for call in self.client.update_story_owner_notification.await_args_list:
            source, record = (*call.args, *call.kwargs.values())[:2]
            if source == story_id:
                written.append(record)
        return written

    def _writes(self, kind: str, source_id: str) -> list[dict]:
        return self._run_writes(source_id) if kind == "run" else self._story_writes(source_id)

    def _current(self, kind: str, source_id: str) -> dict | OwnerNotification | None:
        writes = self._writes(kind, source_id)
        stamp = self._stamps.get((kind, source_id))
        if stamp is not None and stamp[0] == len(writes):
            return stamp[1]
        if writes:
            return writes[-1]
        if kind == "run":
            return self.runs.get(source_id)
        seeded = self.stories.get(source_id)
        if seeded is not None:
            return seeded
        answer = self.client.get_story_owner_notification.return_value
        return answer if isinstance(answer, OwnerNotification) else None

    def _claim(self, kind: str, source_id: str) -> OwnerNotificationAttemptClaim:
        writes = len(self._writes(kind, source_id))
        return claim(
            self.clock,
            lambda: self._current(kind, source_id),
            lambda stamped: self._stamps.__setitem__((kind, source_id), (writes, stamped)),
        )

    async def _claim_run(self, run_id: str) -> OwnerNotificationAttemptClaim:
        return self._claim("run", run_id)

    async def _claim_story(self, story_id: str) -> OwnerNotificationAttemptClaim:
        return self._claim("story", story_id)
