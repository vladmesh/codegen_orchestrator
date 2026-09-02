"""The QA capability calls that invoke a named scheduled behaviour, and read it back.

These drive the one boundary — `build_qa_callables` — the same way both
front-ends do: the central executor's HTTP capability endpoint and the
in-process fallback agent dispatch into this dictionary and can reach nothing
past it. What is asserted here is therefore true of both.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.qa_probe_cli import QA_PROBE_SCRIPT, QA_PROBE_USAGE
from src.agents.qa.capability_service import QACapabilityService
from src.agents.qa.tools import QAJobsCapability, build_qa_callables
from src.clients.product_jobs import GeneratedServiceJobsClient
from src.consumers._qa_target import QACapabilities
from src.consumers._qa_workspace import QAWorkspace
from src.prompts.qa import build_qa_instructions, build_qa_prompt

CAPABILITY = "jobs-capability-never-leaves-the-host"  # noqa: S105
CRITERIA = (
    "- GET /health returns 200\n"
    '- FIRE JOB daily_digest WITH {"chat_id": 42} THEN the bot sends the digest to the owner\n'
)


def _workspace(tmp_path: Path) -> QAWorkspace:
    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    return workspace


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=QACapabilities(
            deployed_url="https://weather.example.com",
            physical_root="/opt/services/weather",
            containers=frozenset({"weather-backend"}),
            loopback_ports=frozenset({8000}),
        )
    )


def _jobs(criteria: str = CRITERIA) -> QAJobsCapability:
    return QAJobsCapability(
        base_url="https://weather.example.com",
        capability=CAPABILITY,
        fired_by_product="project-1",
        fired_by_run="qa-run-7",
        behaviours=tuple(parse_scheduled_behaviours(criteria)),
    )


def _command(**overrides) -> dict:
    return {
        "contract_version": 1,
        "command_id": "qa-qa-run-7-daily_digest",
        "name": "daily_digest",
        "arguments": {"chat_id": 42},
        "fired_by_product": "project-1",
        "fired_by_run": "qa-run-7",
        "dispatch_status": "dispatched",
        "accepted_at": "2026-09-02T10:00:00Z",
        "dispatched_at": "2026-09-02T10:00:01Z",
        **overrides,
    }


def _calls(tmp_path: Path, transport, *, jobs: QAJobsCapability | None = None) -> tuple:
    workspace = _workspace(tmp_path)
    calls = build_qa_callables(
        session=_session(),
        workspace=workspace,
        jobs=jobs if jobs is not None else _jobs(),
        jobs_client_factory=lambda base_url: GeneratedServiceJobsClient(
            base_url, transport=transport
        ),
    )
    return calls, workspace


def _responses(*payloads) -> AsyncMock:
    transport = AsyncMock()
    transport.request.side_effect = list(payloads)
    return transport


def _ok(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://weather.example.com/jobs/fire")
    )


class TestTheNameIsNeverTheExecutorsToChoose:
    @pytest.mark.asyncio
    async def test_a_declared_behaviour_is_fired_with_the_criterias_own_arguments(self, tmp_path):
        transport = _responses(_ok(_command()))
        calls, _ = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        payload = transport.request.call_args.kwargs["json"]
        assert payload["name"] == "daily_digest"
        # The executor supplied only the name; the arguments came off the line
        # that declared the behaviour.
        assert payload["arguments"] == {"chat_id": 42}
        assert answer["dispatch_status"] == "dispatched"
        assert answer["observable"] == "the bot sends the digest to the owner"

    @pytest.mark.asyncio
    async def test_a_name_no_criterion_declared_is_refused_before_anything_is_fired(self, tmp_path):
        transport = _responses()
        calls, _ = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("delete_everything")

        assert "not a scheduled behaviour this run's acceptance criteria named" in answer["error"]
        assert answer["declared_behaviours"] == ["daily_digest"]
        transport.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_run_whose_criteria_name_no_behaviour_gets_no_jobs_calls_at_all(self, tmp_path):
        calls, _ = _calls(tmp_path, _responses(), jobs=_jobs("- GET /health returns 200\n"))

        assert "fire_job" not in calls
        assert "job_evidence" not in calls

    @pytest.mark.asyncio
    async def test_a_deployment_with_no_jobs_capability_offers_no_fire(self, tmp_path):
        calls = build_qa_callables(session=_session(), workspace=_workspace(tmp_path), jobs=None)

        assert "fire_job" not in calls


class TestOneIdentityPerRunPerBehaviour:
    @pytest.mark.asyncio
    async def test_the_same_check_twice_in_a_run_reuses_the_command_identity(self, tmp_path):
        """A retry re-reads one execution; it never causes a second one."""
        transport = _responses(_ok(_command()), _ok(_command()))
        calls, _ = _calls(tmp_path, transport)

        await calls["fire_job"]("daily_digest")
        await calls["fire_job"]("daily_digest")

        identities = {
            call.kwargs["json"]["command_id"] for call in transport.request.call_args_list
        }
        assert identities == {"qa-qa-run-7-daily_digest"}

    @pytest.mark.asyncio
    async def test_evidence_reads_back_the_identity_this_run_fired_under(self, tmp_path):
        transport = _responses(_ok(_command()))
        calls, _ = _calls(tmp_path, transport)

        await calls["job_evidence"]("daily_digest")

        payload = transport.request.call_args.kwargs["json"]
        assert payload == {
            "contract_version": 1,
            "command_id": "qa-qa-run-7-daily_digest",
            "fired_by_product": "project-1",
        }
        assert transport.request.call_args.kwargs["headers"] is None

    @pytest.mark.asyncio
    async def test_the_run_names_itself_as_the_provenance_of_the_fire(self, tmp_path):
        transport = _responses(_ok(_command()))
        calls, _ = _calls(tmp_path, transport)

        await calls["fire_job"]("daily_digest")

        payload = transport.request.call_args.kwargs["json"]
        assert payload["fired_by_run"] == "qa-run-7"
        assert payload["fired_by_product"] == "project-1"


class TestTheCapabilityStaysOnTheManagementHost:
    @pytest.mark.asyncio
    async def test_it_is_a_header_and_is_in_nothing_the_executor_can_see(self, tmp_path):
        transport = _responses(_ok(_command()))
        calls, workspace = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        assert transport.request.call_args.kwargs["headers"] == {"X-Jobs-Capability": CAPABILITY}
        # Everything the executor or a later reader can see.
        assert CAPABILITY not in str(answer)
        assert CAPABILITY not in workspace.trace_text()
        assert CAPABILITY not in workspace.trace_path.read_text()

    def test_no_capability_reaches_the_container_through_the_cli_or_the_prompts(self):
        """The executor's whole vocabulary takes a name, never a credential."""
        prompt = build_qa_prompt(
            CRITERIA,
            "https://weather.example.com",
            established_facts=[],
        )
        for text in (QA_PROBE_SCRIPT, QA_PROBE_USAGE, prompt, build_qa_instructions()):
            assert "X-Jobs-Capability" not in text
            assert "JOBS_FIRE_CAPABILITY" not in text
        assert "qa fire_job NAME" in QA_PROBE_USAGE

    @pytest.mark.asyncio
    async def test_the_capability_endpoint_serves_the_call_by_name_only(self, tmp_path):
        """The one dictionary both front-ends dispatch into takes `name` and nothing else."""
        transport = _responses(_ok(_command()))
        calls, _ = _calls(tmp_path, transport)
        service = QACapabilityService(
            calls=calls,
            capabilities={},
            submit_verdict=lambda raw: None,
            advertised_host="127.0.0.1",
        )

        answer = await service._dispatch("fire_job", {"name": "daily_digest"})

        assert answer["tool"] == "fire_job"
        assert CAPABILITY not in str(answer)
        with pytest.raises(Exception, match="capability"):
            await service._dispatch("fire_job", {"name": "daily_digest", "capability": "x"})


class TestARefusalIsAReadableFailureNotACrash:
    @pytest.mark.asyncio
    async def test_an_undeclared_name_in_the_product_is_a_readable_qa_failure(self, tmp_path):
        transport = _responses(
            httpx.Response(
                404,
                json={"detail": "Job name not declared"},
                request=httpx.Request("POST", "https://weather.example.com/jobs/fire"),
            )
        )
        calls, workspace = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        assert answer["failure"] == "name_not_declared"
        assert "declares no scheduled behaviour by this name" in answer["error"]
        assert "name_not_declared" in workspace.trace_text()

    @pytest.mark.asyncio
    async def test_refused_arguments_are_a_readable_qa_failure(self, tmp_path):
        transport = _responses(
            httpx.Response(
                422,
                json={"detail": "Job arguments do not satisfy the declared schema"},
                request=httpx.Request("POST", "https://weather.example.com/jobs/fire"),
            )
        )
        calls, _ = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        assert answer["failure"] == "arguments_rejected"
        assert "refused the arguments" in answer["error"]

    @pytest.mark.asyncio
    async def test_an_unreachable_product_does_not_raise_into_the_endpoint(self, tmp_path):
        transport = AsyncMock()
        transport.request.side_effect = httpx.ConnectError("no route")
        calls, _ = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        assert answer["failure"] == "transport"


class TestDispatchIsNotProof:
    @pytest.mark.asyncio
    async def test_every_dispatch_record_carries_what_it_does_not_prove(self, tmp_path):
        transport = _responses(_ok(_command()))
        calls, _ = _calls(tmp_path, transport)

        answer = await calls["fire_job"]("daily_digest")

        assert answer["dispatch_status"] == "dispatched"
        assert "not evidence" in answer["dispatch_is_not_proof"]
        assert answer["observable"] == "the bot sends the digest to the owner"

    def test_the_prompt_forbids_passing_a_check_on_the_dispatch_record(self):
        prompt = build_qa_prompt(CRITERIA, "https://weather.example.com", established_facts=[])

        assert "dispatch_status: dispatched" in prompt
        assert "Never pass a check on it" in prompt


class TestTheWriteGuardStillSeesNoApplicationWrite:
    @pytest.mark.asyncio
    async def test_a_sanctioned_fire_is_not_spelled_like_a_forbidden_direct_write(self, tmp_path):
        """The runner's own guard reads this trace; the fire must not trip it."""
        from src.consumers._qa_runner import _forbidden_application_write

        transport = _responses(_ok(_command()))
        calls, workspace = _calls(tmp_path, transport)

        await calls["fire_job"]("daily_digest")

        assert (
            _forbidden_application_write(workspace.trace_text(), "https://weather.example.com")
            is None
        )
