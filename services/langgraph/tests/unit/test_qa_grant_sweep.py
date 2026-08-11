"""The sweep is what makes an ambiguous grant recoverable rather than forgotten.

The runner's `finally` handles the run that ends. These tests are about the ones
that do not: a process killed between writing the record and hearing back from
the install, and a revoke that keeps failing. In both cases the durable record on
the QA run is the only thing that knows a key may be on a target, and the sweep
has to act on it from state alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.dto.qa_ssh_grant import (
    QA_SSH_GRANT_KEY,
    QASshGrant,
    QASshGrantState,
)
from src.consumers._qa_grant_sweep import (
    GRANT_SWEEP_ESCALATE_AFTER,
    GRANT_SWEEP_PAGE,
    sweep_qa_ssh_grants,
)

MARKER = "codegen-qa-run-deadbeef"


def _grant(**overrides) -> QASshGrant:
    base = {
        "marker": MARKER,
        "server_handle": "vps-1",
        "server_ip": "1.2.3.4",
        "ssh_user": "deploy",
        "state": QASshGrantState.ISSUING,
        "issued_at": datetime.now(UTC),
    }
    base.update(overrides)
    return QASshGrant(**base)


EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _run(
    grant: QASshGrant | None, run_id: str = "qa-run-1", created_at: datetime | None = None
) -> SimpleNamespace:
    metadata = {"qa_handoff": {"kept": True}}
    if grant is not None:
        metadata[QA_SSH_GRANT_KEY] = grant.model_dump(mode="json")
    return SimpleNamespace(
        id=run_id, run_metadata=metadata, created_at=created_at or datetime.now(UTC)
    )


def _holders(count: int) -> list[SimpleNamespace]:
    """`count` runs each holding an open grant, oldest first and distinctly aged."""
    return [
        _run(_grant(), run_id=f"qa-run-{i}", created_at=EPOCH + timedelta(minutes=i))
        for i in range(count)
    ]


class _Api:
    """The API surface the sweep uses, recording what it wrote.

    The selection it stands in for is the real one, and it is live: runs whose
    grant record is not `RELEASED`, ordered `(created_at, id)` ascending, cut
    at the requested cursor. A `patch` writing `RELEASED` takes that run out of
    the selection immediately, exactly as the endpoint's predicate does — so
    handling a page here really changes what the next request returns, which is
    the condition the walk has to survive.
    """

    def __init__(self, runs, ssh_key="fleet-key", conflict=False):
        self._runs = list(runs)
        self._ssh_key = ssh_key
        self._conflict = conflict
        self.patches: list[tuple[str, dict]] = []
        self.pages: list[dict] = []
        self.get_server_ssh_key = AsyncMock(return_value=ssh_key)

    def _selected(self) -> list[SimpleNamespace]:
        """The endpoint's predicate: a grant record that is not proven released.

        A record whose state cannot be read has no `state` at all, and it is
        selected for the same reason the endpoint selects it — unreadable is
        not released.
        """
        held = [
            run
            for run in self._runs
            if QA_SSH_GRANT_KEY in run.run_metadata
            and run.run_metadata[QA_SSH_GRANT_KEY].get("state") != QASshGrantState.RELEASED.value
        ]
        return sorted(held, key=lambda run: (run.created_at, run.id))

    async def list_runs_holding_qa_ssh_grant(self, *, limit, after=None):
        self.pages.append({"limit": limit, "after": None if after is None else after.id})
        rows = self._selected()
        if after is not None:
            cursor = (after.created_at, after.id)
            rows = [run for run in rows if (run.created_at, run.id) > cursor]
        return rows[:limit]

    async def patch(self, path, json):
        self.patches.append((path, json))
        if self._conflict and "result" in json:
            response = httpx.Response(409, request=httpx.Request("PATCH", "http://api/" + path))
            raise httpx.HTTPStatusError("settled", request=response.request, response=response)
        # The API merges run_metadata, and the merged record is what the next
        # request selects on.
        if "run_metadata" in json:
            run_id = path.split("/")[1]
            [run] = [r for r in self._runs if r.id == run_id]
            run.run_metadata.update(json["run_metadata"])
        return {}

    def recorded_grant(self) -> QASshGrant:
        payloads = [j for _, j in self.patches if QA_SSH_GRANT_KEY in j.get("run_metadata", {})]
        return QASshGrant.model_validate(payloads[-1]["run_metadata"][QA_SSH_GRANT_KEY])

    def recorded_results(self) -> list[dict]:
        return [j["result"] for _, j in self.patches if "result" in j]


def _sweep(api, revoke):
    return (
        patch("src.consumers._qa_grant_sweep.api_client", api),
        patch("src.consumers._qa_grant_sweep.revoke_grant", revoke),
    )


class TestAmbiguousGrantsAreRecovered:
    async def test_a_record_left_by_a_killed_run_is_revoked_and_closed(self):
        """ISSUING means "a key may be out there", and the sweep resolves it."""
        api = _Api([_run(_grant())])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["revoked"] == 1
        assert revoke.await_args.kwargs["marker"] == MARKER
        assert revoke.await_args.kwargs["ssh_user"] == "deploy"
        assert api.recorded_grant().state is QASshGrantState.RELEASED

    async def test_the_handoff_beside_the_record_is_not_disturbed(self):
        api = _Api([_run(_grant())])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            await sweep_qa_ssh_grants()

        # Only the grant key is written; the API merges run_metadata, so the
        # deploy's handoff plan next to it survives.
        [(_, payload)] = [(p, j) for p, j in api.patches if "run_metadata" in j]
        assert set(payload["run_metadata"]) == {QA_SSH_GRANT_KEY}

    async def test_a_released_record_is_left_alone(self):
        api = _Api([_run(_grant(state=QASshGrantState.RELEASED))])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts == {"seen": 0, "revoked": 0, "failed": 0, "escalated": 0, "unreadable": 0}
        revoke.assert_not_awaited()

    async def test_a_run_without_a_record_is_skipped(self):
        api = _Api([_run(None)])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["seen"] == 0
        revoke.assert_not_awaited()


class TestAGrantThatWillNotGo:
    async def test_a_failed_revoke_counts_up_and_keeps_the_record_open(self):
        api = _Api([_run(_grant(state=QASshGrantState.OPEN))])
        revoke = AsyncMock(return_value="1 authorized_keys line(s) survived revocation")

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["failed"] == 1
        recorded = api.recorded_grant()
        assert recorded.state is QASshGrantState.OPEN
        assert recorded.revoke_attempts == 1
        assert "survived revocation" in recorded.detail
        assert api.recorded_results() == []

    async def test_persistent_residue_becomes_the_runs_reported_outcome(self):
        almost_spent = _grant(
            state=QASshGrantState.OPEN, revoke_attempts=GRANT_SWEEP_ESCALATE_AFTER - 1
        )
        api = _Api([_run(almost_spent)])
        revoke = AsyncMock(return_value="1 authorized_keys line(s) survived revocation")

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["escalated"] == 1
        [result] = api.recorded_results()
        assert result["qa_outcome"] == "blocked"
        assert result["blocker"]["category"] == "qa_cleanup_failed"
        assert MARKER in result["blocker"]["sent"]
        assert result["state_changes"][0]["cleanup"]["succeeded"] is False

    async def test_a_run_that_already_settled_keeps_its_own_outcome(self):
        api = _Api(
            [_run(_grant(state=QASshGrantState.OPEN, revoke_attempts=GRANT_SWEEP_ESCALATE_AFTER))],
            conflict=True,
        )
        revoke = AsyncMock(return_value="still there")

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        # The conflict is information, not a failure: the access is still out
        # and the record stays open for the next cycle.
        assert counts["escalated"] == 1
        assert api.recorded_grant().state is QASshGrantState.OPEN

    async def test_a_raising_revoke_is_a_retry_not_a_crash(self):
        api = _Api([_run(_grant(state=QASshGrantState.OPEN))])
        revoke = AsyncMock(side_effect=OSError("connection refused"))

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["failed"] == 1
        assert "connection refused" in api.recorded_grant().detail

    async def test_a_server_without_a_key_is_recorded_not_raised(self):
        api = _Api([_run(_grant())], ssh_key=None)
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["failed"] == 1
        revoke.assert_not_awaited()
        assert "no server key" in api.recorded_grant().detail


class TestNoOpenRecordIsOutOfReach:
    """Age is not a selection key, and the end of a page is not the end of the work.

    Both of these were one defect: the sweep used to ask for QA runs started in
    the last 24 hours, so an outage longer than that put the record — and the
    `authorized_keys` line it stands for — permanently beyond the only process
    that removes it.

    Selecting by state alone is only half of reach, though. The other half is
    that the walk over that selection has to survive the selection changing
    under it, because the walk is what changes it: closing a record removes it,
    and a cursor counted in rows then steps over the records behind it. So the
    fake below is live — a revoke really does change what the next request
    returns — and one cycle owes every record that was open when it passed.
    """

    async def test_a_grant_older_than_any_window_is_still_revoked(self):
        month_old = _grant(issued_at=datetime.now(UTC) - timedelta(days=30))
        api = _Api([_run(month_old)])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["revoked"] == 1
        assert revoke.await_args.kwargs["marker"] == MARKER
        assert api.recorded_grant().state is QASshGrantState.RELEASED

    async def test_a_month_old_grant_that_will_not_go_reaches_escalation(self):
        month_old = _grant(
            state=QASshGrantState.OPEN,
            issued_at=datetime.now(UTC) - timedelta(days=30),
            revoke_attempts=GRANT_SWEEP_ESCALATE_AFTER - 1,
        )
        api = _Api([_run(month_old)])
        revoke = AsyncMock(return_value="1 authorized_keys line(s) survived revocation")

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["escalated"] == 1
        [result] = api.recorded_results()
        assert result["blocker"]["category"] == "qa_cleanup_failed"

    async def test_the_selection_is_asked_for_by_state_alone(self):
        api = _Api([])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            await sweep_qa_ssh_grants()

        # One page, asked for from the top, with no time bound anywhere in it.
        assert api.pages == [{"limit": GRANT_SWEEP_PAGE, "after": None}]

    async def test_a_record_nothing_can_read_does_not_stop_the_ones_behind_it(self):
        """The selection keeps a malformed record forever, so it cannot be a poison pill.

        A schema change is enough to produce one. If reading it ended the
        cycle, every record after it would stop being reached — the same
        unreachability, arrived at from the other side.
        """
        unreadable = SimpleNamespace(
            id="qa-run-unreadable",
            run_metadata={QA_SSH_GRANT_KEY: {"marker": MARKER, "from": "a schema we lost"}},
            created_at=EPOCH,
        )
        api = _Api(
            [
                unreadable,
                _run(_grant(), run_id="qa-run-behind-it", created_at=EPOCH + timedelta(minutes=1)),
            ]
        )
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["unreadable"] == 1
        assert counts["revoked"] == 1
        assert [p for p, _ in api.patches] == ["runs/qa-run-behind-it"]

    async def test_a_record_past_the_first_page_is_not_left_behind(self):
        """The selection shrinks under the walk, and the walk still presents all of it.

        Every record on the first page is revoked, so by the time the second
        request goes out those rows are `RELEASED` and the fake — like the
        endpoint — no longer selects them. A count-based cursor would ask for
        the rows after the first hundred of a selection that now holds one, get
        nothing back, and end the cycle having left a live key on a target. The
        cursor names the last record handled instead, so the survivor is still
        ahead of it.
        """
        holders = _holders(GRANT_SWEEP_PAGE + 1)
        api = _Api(holders)
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["revoked"] == GRANT_SWEEP_PAGE + 1
        assert {p.split("/")[1] for p, _ in api.patches} == {r.id for r in holders}
        assert api.pages == [
            {"limit": GRANT_SWEEP_PAGE, "after": None},
            {"limit": GRANT_SWEEP_PAGE, "after": holders[GRANT_SWEEP_PAGE - 1].id},
        ]
        # Nothing is left holding a key: the cycle drained the whole selection.
        assert await api.list_runs_holding_qa_ssh_grant(limit=GRANT_SWEEP_PAGE) == []

    async def test_a_record_that_cannot_be_closed_does_not_stall_the_cycle(self):
        """A cursor moves over what it presented, whether or not that record closed.

        An unreadable record is never released, so it is selected on every
        cycle. If the cursor only advanced past records the sweep managed to
        close, the page would come back with that same record at its head
        forever and nothing behind it would ever be reached.
        """
        unreadable = [
            SimpleNamespace(
                id=f"qa-run-unreadable-{i}",
                run_metadata={QA_SSH_GRANT_KEY: {"marker": MARKER, "from": "a schema we lost"}},
                created_at=EPOCH + timedelta(minutes=i),
            )
            for i in range(GRANT_SWEEP_PAGE)
        ]
        behind = _run(
            _grant(),
            run_id="qa-run-behind-them",
            created_at=EPOCH + timedelta(minutes=GRANT_SWEEP_PAGE),
        )
        api = _Api([*unreadable, behind])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["unreadable"] == GRANT_SWEEP_PAGE
        assert counts["revoked"] == 1
        assert [p for p, _ in api.patches] == ["runs/qa-run-behind-them"]


@pytest.mark.parametrize("state", [QASshGrantState.ISSUING, QASshGrantState.OPEN])
def test_every_unreleased_state_is_held(state):
    assert _grant(state=state).held is True


def test_a_released_grant_is_not_held():
    assert _grant(state=QASshGrantState.RELEASED).held is False
