"""Unit tests for resource allocation logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from shared.allocation_disposition import ALLOCATION_DISPOSITIONS, AttemptDisposition
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.server_admission import (
    ADMISSION_FAILURE_REASON,
    ServerAdmissionRejection,
    server_admission_rejection,
    server_admits_application,
)
from shared.tests.server_admission_cases import (
    ADMISSION_CASE_CAPACITY_RAM_MB,
    ADMISSION_CASES,
    CAPACITY_REASONS,
    REFUSED_ADMISSION_CASES,
    admission_case_incidents,
    admission_case_server,
)
from tests.unit.factories import make_server

SERVER = make_server(
    handle="srv-1",
    status="ready",
    public_ip="1.2.3.4",
    capacity_ram_mb=4096,
    capacity_disk_mb=50000,
    last_health_check=datetime.now(UTC),
)

APP = {
    "id": 42,
    "repo_id": "repo-1",
    "server_handle": "srv-1",
    "service_name": "my-bot",
    "status": "running",
    "reserved_ram_mb": 512,
}


def _allocation_settings(*, reserve_mb: int = 256, freshness_seconds: int = 300):
    settings = type("Settings", (), {})()
    settings.allocation_ram_reserve_mb = reserve_mb
    settings.allocation_metrics_freshness_seconds = freshness_seconds
    return settings


def _fresh_server(**overrides):
    return make_server(last_health_check=datetime.now(UTC), **overrides)


class TestEnsureProjectAllocations:
    """Test ensure_project_allocations uses atomic endpoint."""

    @pytest.mark.asyncio
    async def test_creates_application_before_allocating(self):
        """Should create Application first, then allocate ports with application_id."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[])
        mock_client.get_or_create_application = AsyncMock(return_value=APP)
        mock_client.get_application_allocations = AsyncMock(return_value=[])
        mock_client.allocate_next_port = AsyncMock(
            return_value={
                "id": 1,
                "server_handle": "srv-1",
                "port": 8000,
                "service_name": "backend",
                "application_id": 42,
            }
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
            )

        # Application created before allocation
        mock_client.get_or_create_application.assert_called_once_with(
            repo_id="repo-1",
            server_handle="srv-1",
            service_name="my-bot",
            reserved_ram_mb=512,
        )
        mock_client.allocate_next_port.assert_called_once_with(
            "srv-1",
            {
                "service_name": "backend",
                "application_id": 42,
            },
        )
        assert len(result) == 1
        key = list(result.keys())[0]
        assert result[key]["port"] == 8000  # noqa: PLR2004
        assert result[key]["server_ip"] == "1.2.3.4"
        assert result[key]["application_id"] == 42  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_existing_allocations_returned_as_is(self):
        """When allocations already exist, should not call allocate_next_port."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=SERVER)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "port": 8001,
                    "server_ip": "1.2.3.4",
                    "service_name": "backend",
                }
            ]
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot"
            )

        mock_client.allocate_next_port.assert_not_called()
        mock_client.get_or_create_application.assert_not_called()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_existing_allocations_are_extended_for_missing_services(self):
        """Redeploy keeps persisted ports and allocates only newly required services."""
        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=SERVER)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "server_ip": "1.2.3.4",
                    "port": 8000,
                    "service_name": "backend",
                }
            ]
        )
        mock_client.allocate_next_port.side_effect = [{"port": 8001}, {"port": 8002}]

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1",
                repo_id="repo-1",
                service_name="my-bot",
                modules=["backend", "postgres", "redis"],
            )

        assert result["srv-1:8000"]["service_name"] == "backend"
        assert result["srv-1:8001"]["service_name"] == "postgres"
        assert result["srv-1:8002"]["service_name"] == "redis"
        services = [
            call.args[1]["service_name"] for call in mock_client.allocate_next_port.await_args_list
        ]
        assert services == ["postgres", "redis"]

    @pytest.mark.asyncio
    async def test_redeploy_reuses_existing_placement_without_readmitting_ram(self):
        """A deployed project does not need another RAM budget to redeploy."""
        deployed_server = _fresh_server(capacity_ram_mb=1967, used_ram_mb=1300)
        mock_client = AsyncMock()
        mock_client.list_applications = AsyncMock(return_value=[APP])
        mock_client.get_server = AsyncMock(return_value=deployed_server)
        mock_client.get_application_allocations = AsyncMock(
            return_value=[
                {
                    "server_handle": "srv-1",
                    "server_ip": "1.2.3.4",
                    "port": 8000,
                    "service_name": "backend",
                }
            ]
        )

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot"
            )

        assert len(result) == 1
        mock_client.list_servers.assert_not_called()
        mock_client.get_or_create_application.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_modules_allocate_each(self):
        """Each module should get its own atomic allocation."""
        call_count = 0

        mock_client = AsyncMock()
        mock_client.list_servers = AsyncMock(return_value=[SERVER])
        mock_client.list_applications = AsyncMock(return_value=[])
        mock_client.get_or_create_application = AsyncMock(return_value=APP)
        mock_client.get_application_allocations = AsyncMock(return_value=[])

        async def _allocate_next(handle, payload):
            nonlocal call_count
            call_count += 1
            return {
                "id": call_count,
                "server_handle": handle,
                "port": 7999 + call_count,
                "service_name": payload["service_name"],
                "application_id": payload["application_id"],
            }

        mock_client.allocate_next_port = _allocate_next

        with (
            patch("src.allocations.api_client", mock_client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            result = await ensure_project_allocations(
                "proj-1",
                repo_id="repo-1",
                service_name="my-bot",
                modules=["backend", "frontend"],
            )

        assert call_count == 2  # noqa: PLR2004
        assert len(result) == 2  # noqa: PLR2004


class TestSuitableServer:
    """Server selection uses reservations and fresh observed memory together."""

    @pytest.mark.asyncio
    async def test_rejects_server_with_insufficient_observed_free_memory(self):
        server = _fresh_server(capacity_ram_mb=4096, used_ram_mb=3600)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_free_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_rejects_server_with_stale_metrics(self):
        server = make_server(last_health_check=datetime.now(UTC) - timedelta(minutes=10))
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="no_fresh_metrics"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_marks_request_larger_than_every_server_as_impossible(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=0)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="impossible_capacity"):
                await _find_suitable_server(1024, 1024)

    @pytest.mark.asyncio
    async def test_uses_worse_of_reserved_and_observed_memory(self):
        server = _fresh_server(capacity_ram_mb=4500, used_ram_mb=4000)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 3500, "status": "running"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_free_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    async def test_reports_capacity_when_no_server_can_fit_reservations(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=100)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 512, "status": "running"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError, match="insufficient_reserved_memory"):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("used_ram_mb", "applications", "expected"),
        [
            (3600, [], AllocationFailureReason.INSUFFICIENT_FREE_MEMORY),
            (
                100,
                [{"reserved_ram_mb": 3900, "status": "running"}],
                AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY,
            ),
        ],
    )
    async def test_a_full_but_admissible_host_still_reports_capacity(
        self, used_ram_mb, applications, expected
    ):
        """The other half of the rule: a legal host that is out of room says so.

        Reporting every admission refusal as infrastructure must not make the
        capacity reasons unreachable — they are the truth about a host that
        passed admission and then had no memory left.
        """
        server = _fresh_server(capacity_ram_mb=4096, used_ram_mb=used_ram_mb)
        assert server_admits_application(server, frozenset())
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = applications
        client.list_active_incidents.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError) as raised:
                await _find_suitable_server(512, 1024)

        assert raised.value.reason is expected
        assert raised.value.reason in CAPACITY_REASONS

    @pytest.mark.asyncio
    async def test_returns_candidate_with_most_remaining_memory(self):
        smaller = _fresh_server(handle="smaller", capacity_ram_mb=2048, used_ram_mb=600)
        larger = _fresh_server(handle="larger", capacity_ram_mb=4096, used_ram_mb=1000)
        client = AsyncMock()
        client.list_servers.return_value = [smaller, larger]
        client.list_applications.return_value = []

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import _find_suitable_server

            selected = await _find_suitable_server(512, 1024)

        assert selected.handle == "larger"

    @pytest.mark.asyncio
    async def test_ignores_reservation_from_undeployed_application(self):
        server = _fresh_server(capacity_ram_mb=1024, used_ram_mb=100)
        client = AsyncMock()
        client.list_servers.return_value = [server]
        client.list_applications.return_value = [{"reserved_ram_mb": 512, "status": "not_deployed"}]

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import _find_suitable_server

            selected = await _find_suitable_server(512, 1024)

        assert selected is server


def _admission_client(case, now: datetime) -> tuple[AsyncMock, object]:
    """An API client whose whole fleet is the one server this case describes."""
    server = admission_case_server(case, last_health_check=now)
    client = AsyncMock()
    client.list_servers.return_value = [server]
    client.list_applications.return_value = []
    client.list_active_incidents.return_value = admission_case_incidents(case, detected_at=now)
    return client, server


#: The live acceptance state: a managed host whose provisioning has not finished.
STILL_PROVISIONING_CASE = next(
    case for case in ADMISSION_CASES if case.name == "still_provisioning_status"
)

#: Smaller than the 512 MB request plus the 256 MB reserve these tests allocate
#: with, so no admission outcome could make this host fit.
NEVER_FITS_RAM_MB = 640


def _sole_host_client(case, now: datetime, *, capacity_ram_mb: int) -> tuple[AsyncMock, object]:
    """The whole fleet is one server in this state, sized exactly as asked.

    Sizing is a parameter here because the matrix fixtures are deliberately roomy:
    the precedence between "not admissible" and "would never fit" is only visible
    on a host that is both.
    """
    client, server = _admission_client(case, now)
    server = server.model_copy(update={"capacity_ram_mb": capacity_ram_mb})
    client.list_servers.return_value = [server]
    return client, server


def _bound_client(case, now: datetime) -> tuple[AsyncMock, object]:
    """A client whose project is already placed on the server this case describes.

    The reuse shape: an `Application` and its port survive from an earlier
    deploy, and the host they point at has since entered the state under test.
    """
    server = admission_case_server(case, last_health_check=now)
    client = AsyncMock()
    client.list_applications.return_value = [{**APP, "server_handle": server.handle}]
    client.get_server.return_value = server
    client.list_active_incidents.return_value = admission_case_incidents(case, detected_at=now)
    client.get_application_allocations.return_value = [
        {
            "server_handle": server.handle,
            "server_ip": server.public_ip,
            "port": 8000,
            "service_name": "backend",
        }
    ]
    return client, server


class TestProvisioningAdmission:
    """A host may take an application only once its provisioning really finished.

    The states come from `shared.tests.server_admission_cases`, the same table the
    scheduler's resource wait is checked against, so the allocator cannot start
    admitting something the wait does not — or the other way round.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ADMISSION_CASES, ids=lambda case: case.name)
    async def test_allocator_admits_exactly_the_shared_matrix(self, case):
        now = datetime.now(UTC)
        client, server = _admission_client(case, now)

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            if case.admitted:
                assert await _find_suitable_server(512, 1024) is server
                return
            with pytest.raises(AllocationError):
                await _find_suitable_server(512, 1024)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", REFUSED_ADMISSION_CASES, ids=lambda case: case.name)
    async def test_no_refused_host_is_reported_as_a_capacity_shortage(self, case):
        """The machine may not take work — that is never "the request was too big".

        Every refusing state is covered, not just the unfinished builds: a host
        in status `provisioning` or one that stopped being managed says exactly
        as little about memory as a half-installed one does.
        """
        now = datetime.now(UTC)
        client, _ = _admission_client(case, now)

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError) as raised:
                await _find_suitable_server(512, 1024)

        assert raised.value.reason is ADMISSION_FAILURE_REASON
        assert raised.value.reason not in CAPACITY_REASONS

    @pytest.mark.asyncio
    async def test_the_only_server_still_provisioning_is_not_a_memory_shortage(self):
        """The live acceptance run, reproduced.

        A single managed host was still being provisioned when the first
        placement was attempted. The refusal was correct and the wait was
        correct; the reason read `insufficient_free_memory`, on a 4 GB machine
        with nothing on it, because a status rejection fell through to the last
        line of the search.
        """
        now = datetime.now(UTC)
        client, server = _admission_client(STILL_PROVISIONING_CASE, now)
        assert server.used_ram_mb == 0
        assert server.capacity_ram_mb >= 512 + 256

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError) as raised:
                await _find_suitable_server(512, 1024)

        assert raised.value.reason is AllocationFailureReason.SERVER_NOT_PROVISIONED
        disposition = ALLOCATION_DISPOSITIONS[raised.value.reason]
        assert disposition is AttemptDisposition.INFRASTRUCTURE_WAIT

    @pytest.mark.asyncio
    async def test_waiting_out_provisioning_never_makes_a_small_host_bigger(self):
        """Intended precedence: a request nothing could fit is not an admission wait.

        The fleet's only host is both refused admission — still provisioning — and
        physically smaller than the request. `IMPOSSIBLE_CAPACITY` holds only when
        no managed server would fit even fully admitted, so the host that finally
        turns `complete` is still too small and no wait can end well. `OPERATOR_REVIEW`
        fetches a human at once and names the actual blocker: this fleet has no
        machine of the required size.

        This is not the masking the admission reason exists against. Nothing about
        the host's state is retold as a memory shortage; a separate, true and
        durable fact about the fleet is reported instead, and it is more useful than
        parking the request on an event that by definition never arrives.
        """
        now = datetime.now(UTC)
        client, server = _sole_host_client(
            STILL_PROVISIONING_CASE, now, capacity_ram_mb=NEVER_FITS_RAM_MB
        )
        assert (
            server_admission_rejection(server, frozenset())
            is ServerAdmissionRejection.STATUS_NOT_ADMITTING
        )
        assert server.capacity_ram_mb < 512 + 256

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError) as raised:
                await _find_suitable_server(512, 1024)

        assert raised.value.reason is AllocationFailureReason.IMPOSSIBLE_CAPACITY
        assert ALLOCATION_DISPOSITIONS[raised.value.reason] is AttemptDisposition.OPERATOR_REVIEW

    @pytest.mark.asyncio
    async def test_the_same_refused_host_with_room_is_worth_waiting_for(self):
        """The other side of that boundary, one fixture apart from the test above.

        Same host in the same refused state, sized so the request would fit once
        it is admitted. Now provisioning finishing does change the answer, so the
        refusal is the shared admission reason and a bounded infrastructure wait.
        The pair is what says `IMPOSSIBLE_CAPACITY` above is about the fleet's
        sizing and not about the host being inadmissible.
        """
        now = datetime.now(UTC)
        client, server = _sole_host_client(
            STILL_PROVISIONING_CASE, now, capacity_ram_mb=ADMISSION_CASE_CAPACITY_RAM_MB
        )
        assert (
            server_admission_rejection(server, frozenset())
            is ServerAdmissionRejection.STATUS_NOT_ADMITTING
        )
        assert server.capacity_ram_mb >= 512 + 256

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with pytest.raises(AllocationError) as raised:
                await _find_suitable_server(512, 1024)

        assert raised.value.reason is ADMISSION_FAILURE_REASON
        assert raised.value.reason not in CAPACITY_REASONS
        assert (
            ALLOCATION_DISPOSITIONS[raised.value.reason] is AttemptDisposition.INFRASTRUCTURE_WAIT
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", REFUSED_ADMISSION_CASES, ids=lambda case: case.name)
    async def test_both_placement_paths_refuse_with_the_same_reason(self, case):
        """The divergence this card closed must not be expressible again.

        The search for a new host and the re-admission of a bound one are asked
        about the same server state, and their reasons are compared with each
        other rather than with a literal, so a path that grows a reason of its
        own fails here whatever that reason is.
        """
        now = datetime.now(UTC)
        search_client, _ = _admission_client(case, now)
        bound_client, _ = _bound_client(case, now)

        with (
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, _find_suitable_server

            with patch("src.allocations.api_client", search_client):
                with pytest.raises(AllocationError) as searched:
                    await _find_suitable_server(512, 1024)

            from src.allocations import ensure_project_allocations

            with patch("src.allocations.api_client", bound_client):
                with pytest.raises(AllocationError) as bound:
                    await ensure_project_allocations(
                        "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
                    )

        assert searched.value.reason is bound.value.reason
        assert searched.value.reason is ADMISSION_FAILURE_REASON

    @pytest.mark.asyncio
    async def test_only_the_search_answers_a_question_about_the_whole_fleet(self):
        """Where the two paths part, and why that is not the divergence above.

        The same refused host as in the cross-path matrix, now too small to ever
        fit. The search speaks for the fleet: it can see that no machine would take
        this request even fully admitted, and it says so, because no wait resolves
        that. The bound path has one host, already chosen, and nothing to compare it
        with — the fleet question does not exist for it, so it answers the only
        question it has and refuses on admission.

        Both answers are true and neither is a memory shortage. "Both paths report a
        refusal the same way" is about admission, not a rule that the search must
        ignore an impossible request to stay in step with a path that cannot see it.
        """
        now = datetime.now(UTC)
        search_client, _ = _sole_host_client(
            STILL_PROVISIONING_CASE, now, capacity_ram_mb=NEVER_FITS_RAM_MB
        )
        bound_client, bound_server = _bound_client(STILL_PROVISIONING_CASE, now)
        bound_client.get_server.return_value = bound_server.model_copy(
            update={"capacity_ram_mb": NEVER_FITS_RAM_MB}
        )

        with patch("src.allocations.get_settings", return_value=_allocation_settings()):
            from src.allocations import (
                AllocationError,
                _find_suitable_server,
                ensure_project_allocations,
            )

            with patch("src.allocations.api_client", search_client):
                with pytest.raises(AllocationError) as searched:
                    await _find_suitable_server(512, 1024)

            with patch("src.allocations.api_client", bound_client):
                with pytest.raises(AllocationError) as bound:
                    await ensure_project_allocations(
                        "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
                    )

        assert searched.value.reason is AllocationFailureReason.IMPOSSIBLE_CAPACITY
        assert ALLOCATION_DISPOSITIONS[searched.value.reason] is AttemptDisposition.OPERATOR_REVIEW
        assert bound.value.reason is ADMISSION_FAILURE_REASON
        assert bound.value.reason not in CAPACITY_REASONS
        assert ALLOCATION_DISPOSITIONS[bound.value.reason] is AttemptDisposition.INFRASTRUCTURE_WAIT

    @pytest.mark.asyncio
    async def test_provisioned_server_receives_the_application_and_its_ports(self):
        """The end the rule protects: a finished host does get the deployment."""
        case = next(
            candidate
            for candidate in ADMISSION_CASES
            if candidate.name == "provisioned_active_server"
        )
        now = datetime.now(UTC)
        client, server = _admission_client(case, now)
        client.get_or_create_application.return_value = {**APP, "server_handle": server.handle}
        client.get_application_allocations.return_value = []
        client.allocate_next_port.return_value = {
            "port": 8000,
            "server_handle": server.handle,
            "service_name": "backend",
            "application_id": 42,
        }

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import ensure_project_allocations

            allocated = await ensure_project_allocations(
                "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
            )

        assert list(allocated) == [f"{server.handle}:8000"]
        assert allocated[f"{server.handle}:8000"]["server_ip"] == server.public_ip
        client.allocate_next_port.assert_awaited_once_with(
            server.handle,
            {"service_name": "backend", "application_id": 42},
        )


class TestProvisioningAdmissionOnReuse:
    """A project already bound to a host is re-admitted to it, not grandfathered.

    A redeploy runs the application on that host again, and a newly declared
    module takes a fresh port on it: both are placement, so both answer to the
    same table the fresh-placement path answers to. A binding made while the host
    was legal does not survive its provisioning restarting or breaking.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ADMISSION_CASES, ids=lambda case: case.name)
    async def test_reusing_existing_allocations_obeys_the_shared_matrix(self, case):
        """Returning the ports a previous deploy took is still placement."""
        now = datetime.now(UTC)
        client, server = _bound_client(case, now)

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, ensure_project_allocations

            if case.admitted:
                allocated = await ensure_project_allocations(
                    "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
                )
                assert list(allocated) == [f"{server.handle}:8000"]
                return

            with pytest.raises(AllocationError) as raised:
                await ensure_project_allocations(
                    "proj-1", repo_id="repo-1", service_name="my-bot", modules=["backend"]
                )

        # An inadmissible target is infrastructure, so the refusal must not be
        # described as a capacity shortage — and nothing may be handed back that
        # the deploy would then use.
        assert raised.value.reason is AllocationFailureReason.SERVER_NOT_PROVISIONED
        client.get_application_allocations.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ADMISSION_CASES, ids=lambda case: case.name)
    async def test_adding_a_module_obeys_the_shared_matrix(self, case):
        """A new module wants a new port on the bound host — the clearest placement."""
        now = datetime.now(UTC)
        client, server = _bound_client(case, now)
        client.allocate_next_port.return_value = {
            "port": 8001,
            "server_handle": server.handle,
            "service_name": "postgres",
            "application_id": 42,
        }

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, ensure_project_allocations

            if case.admitted:
                allocated = await ensure_project_allocations(
                    "proj-1",
                    repo_id="repo-1",
                    service_name="my-bot",
                    modules=["backend", "postgres"],
                )
                assert allocated[f"{server.handle}:8001"]["service_name"] == "postgres"
                return

            with pytest.raises(AllocationError) as raised:
                await ensure_project_allocations(
                    "proj-1",
                    repo_id="repo-1",
                    service_name="my-bot",
                    modules=["backend", "postgres"],
                )

        assert raised.value.reason is AllocationFailureReason.SERVER_NOT_PROVISIONED
        client.allocate_next_port.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refused_reuse_carries_the_admission_budget_it_asked_for(self):
        """The wait re-checks admission with this budget; a refusal without it stalls."""
        case = next(
            candidate
            for candidate in ADMISSION_CASES
            if candidate.name == "complete_but_provisioning_failed"
        )
        client, _ = _bound_client(case, datetime.now(UTC))

        with (
            patch("src.allocations.api_client", client),
            patch("src.allocations.get_settings", return_value=_allocation_settings()),
        ):
            from src.allocations import AllocationError, ensure_project_allocations

            with pytest.raises(AllocationError) as raised:
                await ensure_project_allocations(
                    "proj-1",
                    repo_id="repo-1",
                    service_name="my-bot",
                    modules=["backend"],
                    min_ram_mb=512,
                    min_disk_mb=1024,
                )

        assert raised.value.required_ram_mb == 512 + 256
        assert raised.value.min_disk_mb == 1024
