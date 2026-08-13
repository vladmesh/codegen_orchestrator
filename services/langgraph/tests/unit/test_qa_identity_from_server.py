"""Where a QA run's identity comes from, and what happens when a host has none.

The run's identity is not `servers.ssh_user`. That column is the administrative
account — `root` on every row `server_sync` writes — and a run performed as it
would hold the platform's own authority over the deployment it is testing. The
identity is an account provisioning creates and records in `servers.labels`, and
these tests drive both halves of that:

* the ordinary path a fresh host takes. The row is built exactly as `server_sync`
  builds it (no `ssh_user`, so `root`), then given the labels the provisioner
  writes when its software phase completes — and exploratory QA runs, as the QA
  account, with the fleet key used only to lend and take back the run's key.
* the host that lends nothing. It is refused before any access is issued, and the
  refusal is written to the provisioning journal against that server handle,
  because "this host has no QA account" is a fact about the build of the host and
  not about the user's project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.server import ServerCreate, ServerDTO, ServerStatus
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.vocab import AgentType
from shared.qa_identity import (
    QA_SSH_USER,
    QA_SSH_USER_LABEL,
    QAIdentityRejection,
    provisioning_complete_labels,
)
from src.consumers._qa_runner import QAResult
from src.consumers.qa import process_qa_job

AGENT_CRITERIA = "- GET /health returns 200\n- the bot answers /start with a greeting"


def _discovered_row() -> ServerCreate:
    """The server row `server_sync` writes when it finds a managed host.

    Copied from `services/scheduler/src/tasks/server_sync.py`: no `ssh_user` is
    passed, so the row carries the column default. That default is `root`, and
    this is the case the QA identity has to survive.
    """
    return ServerCreate(
        handle="vps-267179",
        host="vps-267179.time4vps.cloud",
        public_ip="1.2.3.4",
        is_managed=True,
        status=ServerStatus.PENDING_SETUP,
        labels={"provider_id": "267179"},
    )


def _server(*, provisioned: bool = True, **overrides) -> ServerDTO:
    """That row as the API returns it, after provisioning if `provisioned`."""
    row = _discovered_row()
    labels = dict(row.labels)
    if provisioned:
        labels |= provisioning_complete_labels()
    base = {
        "handle": row.handle,
        "host": row.host,
        "public_ip": row.public_ip,
        "ssh_user": row.ssh_user,
        "status": ServerStatus.ACTIVE,
        "is_managed": True,
        "labels": labels,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


class _TargetWithoutTheAccount:
    """A provisioned host the QA account has since disappeared from.

    The administrative connection still opens — the fleet key works, this is not
    an unreachable machine — and the install script answers the way it answers
    when `getent passwd` finds nobody: exit 3, having appended nothing. The
    revoke that follows reports zero, because a file that is not there holds no
    key.
    """

    def __init__(self) -> None:
        self.appended: list[str] = []

    async def run(self, command, *, check=False, timeout=None):
        if "grep -c -F" in command:
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        return SimpleNamespace(exit_status=3, stdout="", stderr=f"no such account: {QA_SSH_USER}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def qa_message():
    return {
        "story_id": "story-1",
        "project_id": "proj-1",
        "telegram_chat_id": "12345",
        "deployed_url": "https://weather.example.com",
        "application_id": 1,
        "acceptance_criteria": AGENT_CRITERIA,
        "run_id": "qa-run-1",
        "initiating_run_id": "live-1",
        "bot_username": None,
        "qa_attempt": 0,
    }


@pytest.fixture
def api(request):
    """The API as this consumer sees it, answering about one provisioned host."""
    server = getattr(request, "param", None) or _server()
    with patch("src.consumers.qa.api_client") as mock:
        mock.get_project = AsyncMock(
            return_value=ProjectDTO(
                id="116c9678-5872-4ce5-8332-9a267ab27604",
                initiating_run_id="test-run-1",
                title="weather_bot",
                slug="weather-bot-0000",
                status=ProjectStatus.ACTIVE,
                config={},
                owner_id=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        mock.get_application = AsyncMock(
            return_value=ApplicationDTO(
                id=1,
                repo_id="repo-1",
                server_handle=server.handle,
                service_name="weather_bot",
                status="running",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        mock.get_server = AsyncMock(return_value=server)
        mock.get_server_ssh_key = AsyncMock(return_value="-----BEGIN KEY-----\nfleet\n-----")
        mock.patch = AsyncMock(return_value={})
        mock.start_run = AsyncMock(
            return_value=SimpleNamespace(
                run_id="qa-run-1", started=True, run_status=RunStatus.RUNNING
            )
        )
        mock.record_provisioning_failure = AsyncMock(return_value=None)
        yield mock


@pytest.fixture
def redis():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.set = AsyncMock(return_value=True)
    client.redis.delete = AsyncMock()
    return client


@pytest.fixture(autouse=True)
def _runtime_configured():
    """The assigned subscription executor, and no API fallback configured."""
    with patch("src.consumers.qa.get_settings") as get_settings:
        get_settings.return_value = SimpleNamespace(
            qa_executor_agent_type=AgentType.CLAUDE,
            qa_capability_host="qa-worker",
            qa_llm_model=None,
            qa_llm_base_url=None,
            qa_llm_api_key=None,
        )
        yield


@pytest.fixture(autouse=True)
def _url_is_reachable():
    with patch("src.consumers.qa.check_deployed_url_reachable", new_callable=AsyncMock) as check:
        check.return_value = None
        yield


class TestAFreshHostPassesTheOrdinaryPath:
    async def test_a_row_server_sync_creates_has_no_qa_identity_of_its_own(self):
        """The discovered row is `root`, which is why the identity is provisioned."""
        assert _discovered_row().ssh_user == "root"
        assert QA_SSH_USER_LABEL not in _discovered_row().labels

    async def test_after_provisioning_that_host_runs_exploratory_qa(self, api, redis, qa_message):
        with patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock) as run:
            run.return_value = QAResult(passed=True, checks=[], summary="OK")
            result = await process_qa_job(qa_message, redis)

        assert result["status"] == "passed"
        target = run.await_args.kwargs["target"]
        # The run is the provisioned account. The fleet key is passed separately
        # and is used only to lend the run's own key and take it back.
        assert target.qa_ssh_user == QA_SSH_USER
        assert target.ssh_user == "root"
        assert run.await_args.kwargs["fleet_ssh_key"].startswith("-----BEGIN KEY-----")
        api.record_provisioning_failure.assert_not_awaited()


class TestAHostThatLendsNoIdentityIsRefused:
    @pytest.mark.parametrize(
        "api",
        [
            # Provisioned before the QA account existed: phase complete, no label.
            _server(provisioned=False),
            # A label naming the administrative account, which is not weaker than
            # the fleet and so is not an identity a run may borrow.
            _server(labels={QA_SSH_USER_LABEL: "root"}),
            # A label naming an account provisioning did not create. `deploy` is
            # a real account on hosts provisioned by `deploy_target`, it is in
            # the `docker` group there, and `servers.labels` is an untyped dict
            # the server API will PATCH — so if the runtime believed this label
            # it could be pointed at a privileged interactive account and would
            # write a run key into it.
            _server(labels={QA_SSH_USER_LABEL: "deploy"}),
        ],
        indirect=True,
        ids=["no_identity_recorded", "identity_is_the_admin_account", "identity_is_unattested"],
    )
    async def test_the_run_is_blocked_and_no_access_is_issued(self, api, redis, qa_message):
        with patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock) as run:
            result = await process_qa_job(qa_message, redis)

        assert result["status"] == "qa_blocked"
        assert result["blocker"] == QABlockerCategory.SERVER_UNAVAILABLE.value
        # Nothing was installed on the target: the refusal happens before the run.
        run.assert_not_awaited()
        outcome = api.patch.await_args.kwargs["json"]["result"]
        assert outcome["qa_outcome"] == QAOutcome.BLOCKED.value

    @pytest.mark.parametrize("api", [_server(labels={QA_SSH_USER_LABEL: "deploy"})], indirect=True)
    async def test_a_label_naming_another_account_never_reaches_that_account(
        self, api, redis, qa_message
    ):
        """The refusal lands before anything connects to the target.

        `run_qa_centrally` is the only thing that opens the administrative
        connection: it is what issues the one-shot key into the QA account's
        `authorized_keys` and takes it back. Never awaiting it is what "no key
        was written into `deploy`" means from here — and the reason it is refused
        is that provisioning did not write this name, not that this name looks
        dangerous.
        """
        with patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock) as run:
            result = await process_qa_job(qa_message, redis)

        run.assert_not_awaited()
        assert result["status"] == "qa_blocked"
        incident = api.record_provisioning_failure.await_args.args[0]
        assert incident.details["reason"] == QAIdentityRejection.NOT_ATTESTED.value
        assert incident.details["server_handle"] == "vps-267179"

    async def test_the_refusal_is_journalled_as_a_provisioning_fact(self, api, redis, qa_message):
        api.get_server.return_value = _server(provisioned=False)

        with patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock):
            await process_qa_job(qa_message, redis)

        incident = api.record_provisioning_failure.await_args.args[0]
        assert incident.incident_type is IncidentType.PROVISIONING_FAILED
        assert incident.server_handle == "vps-267179"
        assert incident.details["step"] == "qa_identity"
        assert "qa_identity_retrofit vps-267179" in incident.details["repair"]

    async def test_a_host_that_lost_the_account_after_provisioning_is_journalled_too(
        self, api, redis, qa_message, tmp_path
    ):
        """The row is right and the target has drifted. Same fact, same journal.

        This is the host nobody edited: provisioning finished, the label is the
        one provisioning writes, and later the account or its `authorized_keys`
        went away — a home cleaned up, an account removed by hand. The label
        check cannot see that, so the target says it on the install, and the run
        is refused there. What this holds is that the refusal is still a
        provisioning fact naming the handle and the repair, and not only a
        blocked run in a log: those are the two halves of "fail-closed and
        visible", and the second one is the one that used to be missing.
        """
        drifted = _TargetWithoutTheAccount()

        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=drifted)),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
        ):
            result = await process_qa_job(qa_message, redis)

        assert result["status"] == "qa_blocked"
        incident = api.record_provisioning_failure.await_args.args[0]
        assert incident.incident_type is IncidentType.PROVISIONING_FAILED
        assert incident.server_handle == "vps-267179"
        assert incident.details["step"] == "qa_identity"
        assert incident.details["reason"] == QAIdentityRejection.ABSENT_ON_TARGET.value
        assert incident.details["server_handle"] == "vps-267179"
        assert "qa_identity_retrofit vps-267179" in incident.details["repair"]
        # Nothing was created to work around it: the account stays missing.
        assert drifted.appended == []

    async def test_a_journal_that_cannot_be_written_still_refuses_the_run(
        self, api, redis, qa_message
    ):
        """The refusal is fail-closed; the journal write is how it is announced."""
        api.get_server.return_value = _server(provisioned=False)
        api.record_provisioning_failure.side_effect = RuntimeError("journal down")

        with patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock) as run:
            result = await process_qa_job(qa_message, redis)

        assert result["status"] == "qa_blocked"
        run.assert_not_awaited()
