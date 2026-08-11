"""The open-grant selection answers by the state of the record, not by its age.

A QA run writes down the one-shot key it may have installed on a target before
it installs it, and only a readback proving the key gone closes that record. The
sweep that removes such keys reads its work from here, so this endpoint decides
what is recoverable: anything it does not return is a live `authorized_keys`
line nothing will ever come back for.

The previous selection was "QA runs started in the last 24 hours". An outage
longer than the window was enough to lose a record permanently — the key stays
on the target, the record stays unreleased, and neither is ever looked at again.
So the tests below are about age not mattering: a month-old record is work, an
escalated one is still work, and the end of a page is not the end of the
selection.
"""

from datetime import UTC, datetime, timedelta
import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.qa_ssh_grant import QA_SSH_GRANT_KEY, QASshGrant, QASshGrantState
from shared.models import Run

HELD_PATH = "/api/runs/qa-ssh-grants/held"


def _grant(state: QASshGrantState, marker: str, **overrides) -> dict:
    grant = QASshGrant(
        marker=marker,
        server_handle="vps-1",
        server_ip="1.2.3.4",
        ssh_user="qa",
        state=state,
        issued_at=datetime.now(UTC) - timedelta(days=30),
        **overrides,
    )
    return grant.model_dump(mode="json")


async def _qa_run(
    async_client: AsyncClient,
    db_session: AsyncSession,
    *,
    grant: dict | None,
    age: timedelta,
    result: dict | None = None,
) -> str:
    """A QA run of a given age, carrying the grant record it was given."""
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())

    user = await async_client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"grant_{telegram_id}"},
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "QA grant selection", "config": {}},
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED

    run_id = f"qa-{uuid.uuid4().hex[:12]}"
    metadata = {"qa_handoff": {"kept": True}}
    if grant is not None:
        metadata[QA_SSH_GRANT_KEY] = grant
    created = await async_client.post(
        "/api/runs/",
        json={"id": run_id, "type": "qa", "project_id": project_id, "run_metadata": metadata},
    )
    assert created.status_code == status.HTTP_201_CREATED

    if result is not None:
        settled = await async_client.patch(
            f"/api/runs/{run_id}",
            json={"status": "completed", "result": result},
        )
        assert settled.status_code == status.HTTP_200_OK

    # The row is aged in place: the point of every test here is that a record
    # written long before the sweep looks at it is still its work.
    stamp = datetime.now(UTC) - age
    await db_session.execute(
        update(Run).where(Run.id == run_id).values(created_at=stamp, started_at=stamp)
    )
    await db_session.commit()
    return run_id


async def _walk(async_client: AsyncClient, *, limit: int = 100) -> list[str]:
    """Every run the selection hands out, in the order it hands them out."""
    ids: list[str] = []
    offset = 0
    while True:
        page = await async_client.get(HELD_PATH, params={"limit": limit, "offset": offset})
        assert page.status_code == status.HTTP_200_OK
        rows = page.json()
        ids.extend(row["id"] for row in rows)
        if len(rows) < limit:
            return ids
        offset += len(rows)


def _blocked_result(marker: str) -> dict:
    return {
        "qa_outcome": "blocked",
        "summary": "QA left access on the target that could not be proven gone",
        "blocker": {
            "category": "qa_cleanup_failed",
            "attempted": "remove the QA run's one-shot key from the target",
            "sent": f"authorized_keys entry {marker} on 1.2.3.4",
            "received": "the target could not be read back",
        },
    }


@pytest.mark.asyncio
async def test_a_month_old_unreleased_grant_is_still_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """The exact scenario the 24-hour window lost: an outage longer than the window."""
    marker = f"codegen-qa-{uuid.uuid4().hex[:8]}"
    run_id = await _qa_run(
        async_client,
        db_session,
        grant=_grant(QASshGrantState.ISSUING, marker),
        age=timedelta(days=30),
    )

    assert run_id in await _walk(async_client)


@pytest.mark.asyncio
async def test_a_released_grant_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A readback proved the key gone; there is nothing left to take back."""
    marker = f"codegen-qa-{uuid.uuid4().hex[:8]}"
    run_id = await _qa_run(
        async_client,
        db_session,
        grant=_grant(QASshGrantState.RELEASED, marker),
        age=timedelta(days=30),
    )

    assert run_id not in await _walk(async_client)


@pytest.mark.asyncio
async def test_a_run_that_never_held_a_grant_is_not_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    run_id = await _qa_run(async_client, db_session, grant=None, age=timedelta(days=30))

    assert run_id not in await _walk(async_client)


@pytest.mark.asyncio
async def test_an_escalated_run_that_still_holds_its_key_stays_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Escalation reports the residue; it does not make the record go away.

    The run is terminal and already says `qa_cleanup_failed`. The key is still
    on the target, so the record is still work.
    """
    marker = f"codegen-qa-{uuid.uuid4().hex[:8]}"
    run_id = await _qa_run(
        async_client,
        db_session,
        grant=_grant(
            QASshGrantState.OPEN,
            marker,
            revoke_attempts=3,
            detail="1 authorized_keys line(s) survived revocation",
        ),
        age=timedelta(days=45),
        result=_blocked_result(marker),
    )

    assert run_id in await _walk(async_client)


@pytest.mark.asyncio
async def test_a_record_with_no_readable_state_is_still_selected(
    async_client: AsyncClient, db_session: AsyncSession
):
    """Unreadable is not released.

    A record whose shape the current schema cannot parse still stands for a key
    on a target. Selecting only what parses would hide exactly the records that
    most need a human, so it is handed out and the caller decides.
    """
    run_id = await _qa_run(
        async_client,
        db_session,
        grant={"marker": "codegen-qa-from-a-schema-we-lost"},
        age=timedelta(days=30),
    )

    assert run_id in await _walk(async_client)


@pytest.mark.asyncio
async def test_every_open_record_is_handed_out_oldest_first_across_pages(
    async_client: AsyncClient, db_session: AsyncSession
):
    """A page bounds the response, not the coverage, and the oldest goes first."""
    ages = [timedelta(days=90), timedelta(days=60), timedelta(days=30)]
    run_ids = [
        await _qa_run(
            async_client,
            db_session,
            grant=_grant(QASshGrantState.OPEN, f"codegen-qa-{uuid.uuid4().hex[:8]}"),
            age=age,
        )
        for age in ages
    ]

    # One row per page: whatever else the database holds, none of these three
    # may be left past the end of a page.
    handed_out = await _walk(async_client, limit=1)

    assert set(run_ids) <= set(handed_out)
    assert [i for i in handed_out if i in run_ids] == run_ids
