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


def _run(grant: QASshGrant | None, run_id: str = "qa-run-1") -> SimpleNamespace:
    metadata = {"qa_handoff": {"kept": True}}
    if grant is not None:
        metadata[QA_SSH_GRANT_KEY] = grant.model_dump(mode="json")
    return SimpleNamespace(id=run_id, run_metadata=metadata)


class _Api:
    """The API surface the sweep uses, recording what it wrote.

    The selection it stands in for is the real one: runs holding an unreleased
    grant, oldest first, handed out a page at a time. Nothing here knows when a
    run started, because the endpoint the sweep calls does not select on that.
    """

    def __init__(self, runs, ssh_key="fleet-key", conflict=False):
        self._runs = runs
        self._ssh_key = ssh_key
        self._conflict = conflict
        self.patches: list[tuple[str, dict]] = []
        self.pages: list[dict] = []
        self.get_server_ssh_key = AsyncMock(return_value=ssh_key)

    async def list_runs_holding_qa_ssh_grant(self, *, limit, offset):
        self.pages.append({"limit": limit, "offset": offset})
        return self._runs[offset : offset + limit]

    async def patch(self, path, json):
        self.patches.append((path, json))
        if self._conflict and "result" in json:
            response = httpx.Response(409, request=httpx.Request("PATCH", "http://api/" + path))
            raise httpx.HTTPStatusError("settled", request=response.request, response=response)
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
        assert api.pages == [{"limit": GRANT_SWEEP_PAGE, "offset": 0}]

    async def test_a_record_nothing_can_read_does_not_stop_the_ones_behind_it(self):
        """The selection keeps a malformed record forever, so it cannot be a poison pill.

        A schema change is enough to produce one. If reading it ended the
        cycle, every record after it would stop being reached — the same
        unreachability, arrived at from the other side.
        """
        unreadable = SimpleNamespace(
            id="qa-run-unreadable",
            run_metadata={QA_SSH_GRANT_KEY: {"marker": MARKER, "from": "a schema we lost"}},
        )
        api = _Api([unreadable, _run(_grant(), run_id="qa-run-behind-it")])
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["unreadable"] == 1
        assert counts["revoked"] == 1
        assert [p for p, _ in api.patches] == ["runs/qa-run-behind-it"]

    async def test_a_record_past_the_first_page_is_not_left_behind(self):
        holders = [_run(_grant(), run_id=f"qa-run-{i}") for i in range(GRANT_SWEEP_PAGE + 1)]
        api = _Api(holders)
        revoke = AsyncMock(return_value=None)

        with _sweep(api, revoke)[0], _sweep(api, revoke)[1]:
            counts = await sweep_qa_ssh_grants()

        assert counts["revoked"] == GRANT_SWEEP_PAGE + 1
        assert api.pages == [
            {"limit": GRANT_SWEEP_PAGE, "offset": 0},
            {"limit": GRANT_SWEEP_PAGE, "offset": GRANT_SWEEP_PAGE},
        ]
        assert {p.split("/")[1] for p, _ in api.patches} == {r.id for r in holders}


@pytest.mark.parametrize("state", [QASshGrantState.ISSUING, QASshGrantState.OPEN])
def test_every_unreleased_state_is_held(state):
    assert _grant(state=state).held is True


def test_a_released_grant_is_not_held():
    assert _grant(state=QASshGrantState.RELEASED).held is False
