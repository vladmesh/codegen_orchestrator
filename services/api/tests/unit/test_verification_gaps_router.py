"""A project's verification gaps: what a settled QA run writes, once per check.

The endpoints are driven over HTTP against an in-memory session that answers
the queries they make; `tests/service/test_verification_gaps.py` repeats the
round trip against Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.models import Project, Run, User, VerificationGap
from src.database import get_async_session
from src.main import app

PROJECT = uuid.uuid4()
WITHHELD = {
    "name": "criterion not verifiable by QA: - POST /api/transactions returns 201",
    "reason": "needs an HTTP write",
    "origin": "withheld",
}
EXECUTOR = {"name": "upload receipt", "reason": "no tool to upload", "origin": "executor"}


def _run(outcome: str = "passed", status: str = "completed", **result) -> Run:
    return Run(
        id="qa-run-1",
        type="qa",
        status=status,
        project_id=PROJECT,
        story_id="story-1",
        result={"qa_outcome": outcome, "unverified_checks": [WITHHELD, EXECUTOR]}
        | ({"blocker": _blocker()} if outcome == "blocked" else {})
        | result,
    )


def _blocker() -> dict:
    return {"category": "unknown", "attempted": "a", "sent": "s", "received": "r"}


class _Scalars(list):
    def all(self):
        return list(self)


class FakeSession:
    """Just the queries the gap routes make, kept in memory."""

    def __init__(self, *, run: Run | None = None):
        self.project = Project(id=PROJECT, title="weather")
        self.run = run
        self.rows: list[VerificationGap] = []
        self.commits = 0
        self.user = User(id=1, telegram_id=4242, is_admin=False)

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Project:
            return SimpleNamespace(scalar_one_or_none=lambda: self.project)
        if entity is User:
            return SimpleNamespace(scalar_one_or_none=lambda: self.user)
        assert entity is VerificationGap
        return SimpleNamespace(scalars=lambda: _Scalars(self.rows))

    async def get(self, model, key):
        if model is Project:
            return self.project if key == PROJECT else None
        return self.run if self.run is not None and key == self.run.id else None

    def add(self, row):
        # The database stamps `created_at` on insert.
        row.id = 1000 + len(self.rows)
        row.created_at = datetime.now(UTC)
        self.rows.append(row)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def session():
    holder: dict = {}
    app.dependency_overrides[get_async_session] = lambda: holder["session"]
    yield holder
    app.dependency_overrides.pop(get_async_session, None)


async def _post(run_id: str = "qa-run-1", headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers or INTERNAL_HEADERS,
    ) as client:
        return await client.post(
            f"/api/projects/{PROJECT}/verification-gaps/from-run", json={"run_id": run_id}
        )


class TestRecordingFromARun:
    @pytest.mark.parametrize("outcome", ["passed", "failed", "exhausted"])
    async def test_a_settled_verdict_writes_each_unverified_check(self, session, outcome):
        fake = session["session"] = FakeSession(run=_run(outcome))

        response = await _post()

        assert response.status_code == 200
        assert response.json() == {
            "recorded": [WITHHELD["name"], EXECUTOR["name"]],
            "already_recorded": 0,
        }
        assert [
            (row.name, row.reason, row.origin, row.story_id, row.run_id) for row in fake.rows
        ] == [
            (WITHHELD["name"], WITHHELD["reason"], "withheld", "story-1", "qa-run-1"),
            (EXECUTOR["name"], EXECUTOR["reason"], "executor", "story-1", "qa-run-1"),
        ]
        assert fake.commits == 1

    async def test_writing_the_same_run_again_adds_nothing(self, session):
        fake = session["session"] = FakeSession(run=_run())

        await _post()
        again = await _post()

        assert again.json() == {"recorded": [], "already_recorded": 2}
        assert len(fake.rows) == 2

    @pytest.mark.parametrize(
        ("outcome", "status"),
        [("blocked", "completed"), ("error", "failed"), ("passed", "running")],
    )
    async def test_a_run_without_a_verdict_records_no_gaps(self, session, outcome, status):
        fake = session["session"] = FakeSession(run=_run(outcome, status))

        response = await _post()

        assert response.status_code == 409
        assert fake.rows == []

    async def test_only_the_qa_runtime_writes_gaps(self, session):
        session["session"] = FakeSession(run=_run())

        response = await _post(headers={**INTERNAL_HEADERS, "X-Telegram-ID": "4242"})

        assert response.status_code == 403

    async def test_a_run_of_another_project_is_refused(self, session):
        session["session"] = FakeSession(run=_run(project_id=None))
        session["session"].run.project_id = uuid.uuid4()

        response = await _post()

        assert response.status_code == 409


class TestReadingThem:
    async def test_the_admin_api_lists_a_projects_gaps(self, session):
        session["session"] = FakeSession(run=_run())
        await _post()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            listed = await client.get(f"/api/projects/{PROJECT}/verification-gaps")

        assert listed.status_code == 200
        assert [(gap["name"], gap["origin"]) for gap in listed.json()] == [
            (WITHHELD["name"], "withheld"),
            (EXECUTOR["name"], "executor"),
        ]
