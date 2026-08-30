"""Tests for shared infra_client."""

import pytest

from shared.clients.infra_client import check_http_health


class TestCheckHttpHealth:
    @pytest.mark.asyncio
    async def test_healthy(self):
        import httpx
        import respx

        with respx.mock:
            respx.get("http://1.2.3.4:8080/health").mock(return_value=httpx.Response(200))
            result = await check_http_health("http://1.2.3.4:8080/health")

        assert result["healthy"] is True
        assert result["status_code"] == 200  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        import httpx
        import respx

        with respx.mock:
            respx.get("http://1.2.3.4:8080/health").mock(return_value=httpx.Response(500))
            result = await check_http_health("http://1.2.3.4:8080/health")

        assert result["healthy"] is False
        assert result["status_code"] == 500  # noqa: PLR2004
