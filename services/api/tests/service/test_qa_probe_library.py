"""A passed QA run's probes land in its project's library, and the admin API reads them back."""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest


async def _qa_run(async_client: AsyncClient) -> tuple[str, str, int]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    user = await async_client.post(
        "/api/users/",
        json={
            "telegram_id": telegram_id,
            "username": f"library_{telegram_id}",
            "is_admin": True,
        },
    )
    assert user.status_code == status.HTTP_201_CREATED
    project = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "Probe Library",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED
    run_id = f"qa-{uuid.uuid4().hex[:8]}"
    run = await async_client.post(
        "/api/work-admission/paid-runs",
        json={"id": run_id, "type": "qa", "project_id": project_id},
    )
    assert run.status_code == status.HTTP_200_OK
    return project_id, run_id, telegram_id


def _probe(name: str, **overrides) -> dict:
    record = {
        "id": f"probe-{name}",
        "platform": "http",
        "name": name,
        "source": f"print({name!r})",
        "arguments": [],
        "stdout": "ok",
        "stderr": "",
        "exit_status": 0,
        "duration_ms": 3,
        "source_truncated": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "file_kind": "py",
    }
    record.update(overrides)
    return record


@pytest.mark.asyncio
async def test_a_passed_runs_probes_are_stored_and_read_back(async_client: AsyncClient):
    project_id, run_id, admin_id = await _qa_run(async_client)
    settled = await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {
                "qa_outcome": "passed",
                "probe_runs": [
                    _probe("health", source="first"),
                    _probe("broken", exit_status=1),
                    _probe("cut", source_truncated=True),
                    _probe("location", platform="telegram"),
                    _probe("health", source="second"),
                ],
            },
        },
    )
    assert settled.status_code == status.HTTP_200_OK

    stored = await async_client.post(
        f"/api/projects/{project_id}/qa-probes/from-run", json={"run_id": run_id}
    )

    assert stored.status_code == status.HTTP_200_OK
    assert sorted(stored.json()["stored"]) == ["http/health", "telegram/location"]
    library = await async_client.get(
        f"/api/projects/{project_id}/qa-probes", headers={"X-Telegram-ID": str(admin_id)}
    )
    assert library.status_code == status.HTTP_200_OK
    entries = {(entry["platform"], entry["name"]): entry for entry in library.json()}
    assert set(entries) == {("http", "health"), ("telegram", "location")}
    assert entries[("http", "health")]["source"] == "second"
    assert entries[("http", "health")]["origin_run_id"] == run_id
    assert entries[("http", "health")]["file_kind"] == "py"
    assert entries[("http", "health")]["project_id"] == project_id

    # Storing the same run again updates in place: still one row per name.
    again = await async_client.post(
        f"/api/projects/{project_id}/qa-probes/from-run", json={"run_id": run_id}
    )
    assert again.status_code == status.HTTP_200_OK
    assert len((await async_client.get(f"/api/projects/{project_id}/qa-probes")).json()) == 2


@pytest.mark.asyncio
async def test_a_failed_run_stores_nothing_and_projects_do_not_share(async_client: AsyncClient):
    project_id, run_id, _ = await _qa_run(async_client)
    other_project, other_run, _ = await _qa_run(async_client)
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {"qa_outcome": "failed", "probe_runs": [_probe("health")]},
        },
    )
    await async_client.patch(
        f"/api/runs/{other_run}",
        json={
            "status": "completed",
            "result": {"qa_outcome": "passed", "probe_runs": [_probe("theirs")]},
        },
    )

    refused = await async_client.post(
        f"/api/projects/{project_id}/qa-probes/from-run", json={"run_id": run_id}
    )
    crossed = await async_client.post(
        f"/api/projects/{project_id}/qa-probes/from-run", json={"run_id": other_run}
    )
    theirs = await async_client.post(
        f"/api/projects/{other_project}/qa-probes/from-run", json={"run_id": other_run}
    )

    assert refused.status_code == status.HTTP_409_CONFLICT
    assert crossed.status_code == status.HTTP_409_CONFLICT
    assert theirs.status_code == status.HTTP_200_OK
    assert (await async_client.get(f"/api/projects/{project_id}/qa-probes")).json() == []
    [entry] = (await async_client.get(f"/api/projects/{other_project}/qa-probes")).json()
    assert entry["name"] == "theirs"


@pytest.mark.asyncio
async def test_a_deleted_project_takes_its_library_with_it(async_client: AsyncClient):
    project_id, run_id, _ = await _qa_run(async_client)
    await async_client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": "completed",
            "result": {"qa_outcome": "passed", "probe_runs": [_probe("health")]},
        },
    )
    await async_client.post(
        f"/api/projects/{project_id}/qa-probes/from-run", json={"run_id": run_id}
    )

    deleted = await async_client.delete(f"/api/projects/{project_id}")

    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text
