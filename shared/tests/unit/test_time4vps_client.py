"""Time4VPS client: 4xx/5xx responses must surface the provider's own reason."""

from unittest.mock import patch

import httpx
import pytest
from structlog.testing import capture_logs

from shared.clients.time4vps import Time4VPSAPIError, Time4VPSClient

# Body the provider returns for a correct login coming from an unlisted address.
_IP_NOT_ALLOWED = '{"error":["ipnotallowed","unauthorized"]}'
_WRONG_LOGIN = '{"error":["wronglogin","unauthorized"]}'


class _StubAsyncClient:
    """Stands in for httpx.AsyncClient, answering every request with one response."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url))
        return self._response


def _stub(status_code: int, body: str) -> _StubAsyncClient:
    return _StubAsyncClient(
        httpx.Response(
            status_code,
            text=body,
            request=httpx.Request("GET", "https://billing.time4vps.com/api/server"),
        )
    )


@pytest.mark.asyncio
async def test_error_body_is_logged_with_status():
    client = Time4VPSClient("user", "secret")

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", _stub(401, _WRONG_LOGIN)),
        capture_logs() as logs,
        pytest.raises(Time4VPSAPIError),
    ):
        await client.get_servers()

    errors = [entry for entry in logs if entry["event"] == "time4vps_http_error"]
    assert len(errors) == 1
    assert errors[0]["status_code"] == 401  # noqa: PLR2004
    assert errors[0]["body"] == _WRONG_LOGIN


@pytest.mark.asyncio
async def test_error_body_travels_on_the_exception():
    client = Time4VPSClient("user", "secret")

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", _stub(401, _IP_NOT_ALLOWED)),
        pytest.raises(Time4VPSAPIError) as excinfo,
    ):
        await client.get_servers()

    assert excinfo.value.status_code == 401  # noqa: PLR2004
    assert excinfo.value.body == _IP_NOT_ALLOWED
    # Callers that only log str(exc) still see the reason.
    assert "ipnotallowed" in str(excinfo.value)


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("get_servers", ()),
        ("get_server_details", (7,)),
        ("reset_password", (7,)),
        ("get_task_result", (7, 42)),
        ("get_available_os_templates", (7,)),
        ("reinstall_server", (7, "kvm-ubuntu-24.04-gpt-x86_64")),
    ],
)
@pytest.mark.asyncio
async def test_every_endpoint_reports_the_body(call, args):
    client = Time4VPSClient("user", "secret")

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", _stub(500, "upstream exploded")),
        capture_logs() as logs,
        pytest.raises(Time4VPSAPIError) as excinfo,
    ):
        await getattr(client, call)(*args)

    assert excinfo.value.body == "upstream exploded"
    assert [entry["body"] for entry in logs if entry["event"] == "time4vps_http_error"] == [
        "upstream exploded"
    ]


@pytest.mark.asyncio
async def test_oversized_error_body_is_truncated():
    client = Time4VPSClient("user", "secret")
    body = "x" * 5000

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", _stub(502, body)),
        pytest.raises(Time4VPSAPIError) as excinfo,
    ):
        await client.get_servers()

    assert len(excinfo.value.body) == 2000  # noqa: PLR2004


@pytest.mark.asyncio
async def test_successful_response_is_parsed():
    payload = [{"id": 1001, "ip": "1.2.3.4", "domain": "host.example"}]
    stub = _StubAsyncClient(
        httpx.Response(
            200,
            json=payload,
            request=httpx.Request("GET", "https://billing.time4vps.com/api/server"),
        )
    )

    client = Time4VPSClient("user", "secret")
    with patch("shared.clients.time4vps.httpx.AsyncClient", stub):
        servers = await client.get_servers()

    assert [s.id for s in servers] == [1001]
    assert stub.calls == [("GET", "https://billing.time4vps.com/api/server")]
