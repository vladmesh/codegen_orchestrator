"""Unit tests for port allocation endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app

APPLICATION_ID = 42


def _mock_session(
    server_exists=True,
    existing_allocation=None,
    allocated_ports=None,
):
    """Create a mock DB session for port allocation tests."""
    session = AsyncMock()

    # db.get(Server, handle) for server existence check
    server = MagicMock() if server_exists else None
    session.get = AsyncMock(return_value=server)

    # Build port rows as (port,) tuples for the SELECT query
    port_rows = []
    if allocated_ports is not None:
        port_rows = [(alloc.port,) for alloc in allocated_ports]

    # For queries — need to handle both SELECT port queries and scalar queries
    def _make_execute():
        call_count = 0

        async def _execute(query, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            result_mock = MagicMock()
            # The allocate-next endpoint does result.all() on port tuples
            result_mock.all = MagicMock(return_value=port_rows)
            # For older endpoints that use scalar_one_or_none
            result_mock.scalar_one_or_none = MagicMock(return_value=existing_allocation)
            scalars_mock = MagicMock()
            if allocated_ports is not None:
                scalars_mock.all = MagicMock(return_value=allocated_ports)
            result_mock.scalars = MagicMock(return_value=scalars_mock)
            return result_mock

        return _execute

    session.execute = _make_execute()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def _refresh(obj):
        obj.id = 1
        now = datetime.now(UTC)
        if not getattr(obj, "created_at", None):
            obj.created_at = now
        if not getattr(obj, "updated_at", None):
            obj.updated_at = now

    session.refresh = _refresh

    async def _session_gen():
        yield session

    return session, _session_gen


def _make_allocation(
    server_handle="srv-1", port=8000, service_name="backend", application_id=APPLICATION_ID
):
    alloc = MagicMock()
    alloc.id = 1
    alloc.server_handle = server_handle
    alloc.port = port
    alloc.service_name = service_name
    alloc.application_id = application_id
    return alloc


class TestPortAllocationModel:
    """Test that PortAllocation model has correct structure."""

    def test_unique_constraint_on_server_handle_port(self):
        from sqlalchemy import UniqueConstraint as UC

        from shared.models.port_allocation import PortAllocation

        table = PortAllocation.__table__
        unique_constraints = [uc for uc in table.constraints if isinstance(uc, UC)]
        # Find a unique constraint that covers (server_handle, port)
        found = any(
            {col.name for col in uc.columns} == {"server_handle", "port"}
            for uc in unique_constraints
        )
        assert found, "PortAllocation must have a UniqueConstraint on (server_handle, port)"

    def test_has_application_id_fk(self):
        """PortAllocation must have application_id FK to applications."""
        from shared.models.port_allocation import PortAllocation

        col = PortAllocation.__table__.c.application_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "applications.id" in fk_targets

    def test_no_project_id_column(self):
        """PortAllocation should NOT have project_id — ports belong to Application."""
        from shared.models.port_allocation import PortAllocation

        cols = {c.name for c in PortAllocation.__table__.columns}
        assert "project_id" not in cols


class TestAllocateNextPort:
    """Test POST /{handle}/ports/allocate-next endpoint."""

    @pytest.mark.asyncio
    async def test_allocate_next_returns_first_available(self):
        """When no ports allocated, should return start_port (8000)."""
        session, session_gen = _mock_session(server_exists=True, allocated_ports=[])

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/servers/srv-1/ports/allocate-next",
                    json={
                        "service_name": "backend",
                        "application_id": APPLICATION_ID,
                    },
                )
            assert resp.status_code == 200, resp.text  # noqa: PLR2004
            data = resp.json()
            assert data["port"] >= 8000  # noqa: PLR2004
            assert data["server_handle"] == "srv-1"
            assert data["service_name"] == "backend"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_allocate_next_skips_taken_ports(self):
        """When ports 8000-8002 are taken, should allocate 8003."""
        taken = []
        for p in [8000, 8001, 8002]:
            alloc = MagicMock()
            alloc.port = p
            taken.append(alloc)

        session, session_gen = _mock_session(server_exists=True, allocated_ports=taken)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/servers/srv-1/ports/allocate-next",
                    json={
                        "service_name": "backend",
                        "application_id": APPLICATION_ID,
                    },
                )
            assert resp.status_code == 200, resp.text  # noqa: PLR2004
            data = resp.json()
            assert data["port"] == 8003  # noqa: PLR2004
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_allocate_next_server_not_found(self):
        """Should return 404 when server doesn't exist."""
        session, session_gen = _mock_session(server_exists=False)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/servers/nonexistent/ports/allocate-next",
                    json={
                        "service_name": "backend",
                        "application_id": APPLICATION_ID,
                    },
                )
            assert resp.status_code == 404  # noqa: PLR2004
        finally:
            app.dependency_overrides.clear()
