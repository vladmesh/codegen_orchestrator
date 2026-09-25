"""A project's QA probe library: what a passed run stores, and the cap that bounds it.

The endpoint is driven over HTTP against an in-memory session that answers the
four queries it makes; `tests/service/test_qa_probe_library.py` repeats the
round trip against Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.qa_probe_library import QA_PROBE_LIBRARY_CAP
from shared.contracts.dto.run_result import QARunResult
from shared.models import Project, QAProbe, Run, User
from src.database import get_async_session
from src.main import app
from src.routers.projects.qa_probes import _library_candidates, _over_cap

PROJECT = uuid.uuid4()


def _probe(name: str, **overrides) -> dict:
    record = {
        "id": f"probe-{name}",
        "platform": "http",
        "name": name,
        "source": f"print({name!r})",
        "arguments": [],
        "stdout": "",
        "stderr": "",
        "exit_status": 0,
        "duration_ms": 1,
        "file_kind": "py",
    }
    record.update(overrides)
    return record


def _run(*probes: dict, outcome: str = "passed", status: str = "completed", **overrides) -> Run:
    values = {
        "id": "qa-run-1",
        "type": "qa",
        "status": status,
        "project_id": PROJECT,
        "result": {"qa_outcome": outcome, "probe_runs": list(probes)}
        | ({"blocker": _blocker()} if outcome == "blocked" else {}),
    }
    values.update(overrides)
    return Run(**values)


def _blocker() -> dict:
    return {"category": "unknown", "attempted": "a", "sent": "s", "received": "r"}


class _Scalars(list):
    def all(self):
        return list(self)


class FakeSession:
    """Just the queries the library routes make, kept in memory."""

    def __init__(self, *, run: Run | None = None, rows: list[QAProbe] | None = None):
        self.project = Project(id=PROJECT, title="weather")
        self.run = run
        self.rows = list(rows or [])
        self.deleted: list[QAProbe] = []
        self.committed = False
        self.user = User(id=1, telegram_id=4242, is_admin=False)

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Project:
            return SimpleNamespace(scalar_one_or_none=lambda: self.project)
        if entity is User:
            return SimpleNamespace(scalar_one_or_none=lambda: self.user)
        assert entity is QAProbe
        return SimpleNamespace(scalars=lambda: _Scalars(self.rows))

    async def get(self, model, key):
        if model is Project:
            return self.project if key == PROJECT else None
        return self.run if self.run is not None and key == self.run.id else None

    def add(self, row):
        row.id = 1000 + len(self.rows)
        self.rows.append(row)

    async def flush(self):
        return None

    async def delete(self, row):
        self.rows.remove(row)
        self.deleted.append(row)

    async def commit(self):
        self.committed = True


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
            f"/api/projects/{PROJECT}/qa-probes/from-run", json={"run_id": run_id}
        )


class TestWhatAPassedRunContributes:
    def test_exit_zero_whole_source_known_kind_and_the_last_record_of_a_name(self):
        result = QARunResult.model_validate(
            {
                "qa_outcome": "passed",
                "probe_runs": [
                    _probe("health", source="first"),
                    _probe("broken", exit_status=1),
                    _probe("cut", source_truncated=True),
                    _probe("legacy", file_kind=None),
                    _probe("health", source="second"),
                    _probe("location", platform="telegram"),
                ],
            }
        )

        candidates, skipped = _library_candidates(result)

        assert skipped == 0
        assert list(candidates) == [("http", "health"), ("telegram", "location")]
        assert candidates[("http", "health")].source == "second"

    def test_the_oldest_update_goes_first_past_the_cap(self):
        now = datetime.now(UTC)
        rows = [
            QAProbe(id=index, name=f"p{index}", updated_at=now - timedelta(minutes=index))
            for index in range(QA_PROBE_LIBRARY_CAP + 2)
        ]

        evicted = _over_cap(rows)

        assert [row.name for row in evicted] == [
            f"p{QA_PROBE_LIBRARY_CAP}",
            f"p{QA_PROBE_LIBRARY_CAP + 1}",
        ]


class TestStoringFromARun:
    async def test_a_passed_run_upserts_its_eligible_probes(self, session):
        stale = QAProbe(
            id=1,
            project_id=PROJECT,
            platform="http",
            name="health",
            source="old",
            file_kind="sh",
            origin_run_id="qa-run-0",
            updated_at=datetime.now(UTC) - timedelta(days=1),
        )
        fake = session["session"] = FakeSession(
            run=_run(_probe("health", source="new"), _probe("broken", exit_status=2)),
            rows=[stale],
        )

        response = await _post()

        assert response.status_code == 200
        assert response.json() == {"stored": ["http/health"], "evicted": [], "skipped": 0}
        [row] = fake.rows
        assert (row.source, row.file_kind, row.origin_run_id) == ("new", "py", "qa-run-1")
        assert fake.committed

    async def test_storing_past_the_cap_evicts_the_oldest_entries(self, session):
        old = datetime.now(UTC) - timedelta(days=1)
        rows = [
            QAProbe(
                id=index,
                project_id=PROJECT,
                platform="web",
                name=f"old-{index}",
                source="x",
                file_kind="py",
                origin_run_id="qa-run-0",
                updated_at=old + timedelta(seconds=index),
            )
            for index in range(QA_PROBE_LIBRARY_CAP)
        ]
        fake = session["session"] = FakeSession(run=_run(_probe("fresh")), rows=rows)

        response = await _post()

        assert response.json() == {
            "stored": ["http/fresh"],
            "evicted": ["web/old-0"],
            "skipped": 0,
        }
        assert len(fake.rows) == QA_PROBE_LIBRARY_CAP
        assert [row.name for row in fake.deleted] == ["old-0"]

    @pytest.mark.parametrize(
        ("name", "kept"),
        [
            ("a b", False),
            ("it's", False),
            ('say"hi"', False),
            ("ctl\x01char", False),
            ("tab\there", False),
            ("line\n", False),
            ("x" * 256, False),
            ("x" * 65, False),
            ("../x", False),
            ("a/b", False),
            (".hidden", False),
            ("-flag", False),
            ("ÿ", False),
            # The first review's collision pair: the spaced name is skipped,
            # and the one that looked like its digest stem is just a name.
            ("a_b-c8687a08", True),
            ("x" * 64, True),
            ("health.v2", True),
            ("Check_1-a", True),
        ],
    )
    async def test_a_name_that_is_not_a_library_name_is_skipped_and_counted(
        self, session, name, kept
    ):
        fake = session["session"] = FakeSession(run=_run(_probe(name), _probe("health")))

        response = await _post()

        assert response.status_code == 200
        stored = ["http/health", f"http/{name}"] if kept else ["http/health"]
        assert sorted(response.json()["stored"]) == sorted(stored)
        assert response.json()["skipped"] == (0 if kept else 1)
        assert sorted(row.name for row in fake.rows) == sorted(
            [name, "health"] if kept else ["health"]
        )

    async def test_the_reviewers_collision_pair_stores_one_entry(self, session):
        fake = session["session"] = FakeSession(
            run=_run(_probe("a b"), _probe("a_b-c8687a08")),
        )

        response = await _post()

        assert response.json() == {"stored": ["http/a_b-c8687a08"], "evicted": [], "skipped": 1}
        assert [row.name for row in fake.rows] == ["a_b-c8687a08"]

    async def test_the_reviewers_overflow_names_store_nothing(self, session):
        fake = session["session"] = FakeSession(
            run=_run(*(_probe("'" * 250 + f"{index:03}") for index in range(QA_PROBE_LIBRARY_CAP))),
        )

        response = await _post()

        assert response.json() == {"stored": [], "evicted": [], "skipped": QA_PROBE_LIBRARY_CAP}
        assert fake.rows == []

    @pytest.mark.parametrize(
        "run",
        [
            _run(_probe("health"), outcome="failed"),
            _run(_probe("health"), outcome="blocked"),
            _run(_probe("health"), outcome="exhausted"),
            _run(_probe("health"), status="running"),
            _run(_probe("health"), type="engineering"),
            _run(_probe("health"), project_id=uuid.uuid4()),
            _run(result=None),
        ],
    )
    async def test_anything_but_a_settled_pass_of_this_project_stores_nothing(self, session, run):
        fake = session["session"] = FakeSession(run=run)

        response = await _post()

        assert response.status_code == 409
        assert fake.rows == []
        assert not fake.committed

    async def test_an_unknown_run_is_not_found(self, session):
        session["session"] = FakeSession(run=None)

        assert (await _post("qa-run-404")).status_code == 404

    async def test_only_the_qa_runtime_may_store(self, session):
        fake = session["session"] = FakeSession(run=_run(_probe("health")))

        response = await _post(headers={**INTERNAL_HEADERS, "X-Telegram-ID": "4242"})

        assert response.status_code == 403
        assert fake.rows == []


class TestReadingTheLibrary:
    async def test_the_admin_api_lists_a_projects_entries(self, session):
        now = datetime.now(UTC)
        session["session"] = FakeSession(
            rows=[
                QAProbe(
                    id=1,
                    project_id=PROJECT,
                    platform="telegram",
                    name="location",
                    source="print('x')",
                    file_kind="py",
                    origin_run_id="qa-run-1",
                    created_at=now,
                    updated_at=now,
                )
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
        ) as client:
            response = await client.get(f"/api/projects/{PROJECT}/qa-probes")

        assert response.status_code == 200
        [entry] = response.json()
        assert entry["project_id"] == str(PROJECT)
        assert (entry["platform"], entry["name"], entry["file_kind"]) == (
            "telegram",
            "location",
            "py",
        )
        assert entry["origin_run_id"] == "qa-run-1"

    async def test_a_non_admin_user_cannot_read_it(self, session):
        session["session"] = FakeSession()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={**INTERNAL_HEADERS, "X-Telegram-ID": "4242"},
        ) as client:
            response = await client.get(f"/api/projects/{PROJECT}/qa-probes")

        assert response.status_code == 403
