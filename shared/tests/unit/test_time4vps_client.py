"""Time4VPS client: 4xx/5xx responses must surface the provider's own reason."""

from unittest.mock import patch

import httpx
import pytest
from structlog.testing import capture_logs

from shared.clients.time4vps import Time4VPSAPIError, Time4VPSClient

# Body the provider returns for a correct login coming from an unlisted address.
_IP_NOT_ALLOWED = '{"error":["ipnotallowed","unauthorized"]}'
_WRONG_LOGIN = '{"error":["wronglogin","unauthorized"]}'
# Observed on vps-275301, 2026-08-06 10:01:29 UTC: the billing API throttling a poll
# 11 seconds before the very task it refused to report on completed successfully.
_RATE_LIMITED = '{"error":[["wait_x_between_action",24],"unauthorized"]}'


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


class _SequenceAsyncClient(_StubAsyncClient):
    """Answers each request with the next response of a fixed script."""

    def __init__(self, responses: list[httpx.Response]):
        super().__init__(responses[0])
        self._responses = list(responses)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url))
        return self._responses.pop(0)


class _FakeAsyncio:
    """Stand-in for the module's `asyncio` use: a clock that only sleep advances.

    The client reads time via `asyncio.get_running_loop().time()` and waits via
    `asyncio.sleep`, so a fake of both keeps the timeout budget observable without
    the test spending the seconds it asserts on.
    """

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def get_running_loop(self):
        return self

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _task(completed: str | None, results: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "name": "server_recreate",
            "activated": "2026-08-06 09:56:00",
            "completed": completed,
            "results": results,
        },
        request=httpx.Request("GET", "https://billing.time4vps.com/api/server/275301/task/4948782"),
    )


def _error(status_code: int, body: str) -> httpx.Response:
    return httpx.Response(
        status_code,
        text=body,
        request=httpx.Request("GET", "https://billing.time4vps.com/api/server/275301/task/4948782"),
    )


_NEW_PASSWORD = "Xk9mP3qR7"  # noqa: S105 — fixture value, not a credential
_REINSTALL_RESULTS = f"Password: \t<a onclick='this.innerHTML = \"{_NEW_PASSWORD}\"'>Reveal</a>"


def test_rate_limit_is_told_from_lost_authorization_by_the_body():
    """The status code carries nothing here — all three of these are 401."""
    url = "https://billing.time4vps.com/api/server/275301/task/4948782"

    assert Time4VPSAPIError("GET", url, 401, _RATE_LIMITED).rate_limit_wait_seconds == 24  # noqa: PLR2004
    assert Time4VPSAPIError("GET", url, 401, _IP_NOT_ALLOWED).rate_limit_wait_seconds is None
    assert Time4VPSAPIError("GET", url, 401, _WRONG_LOGIN).rate_limit_wait_seconds is None


@pytest.mark.asyncio
async def test_rate_limited_poll_waits_the_stated_interval_and_still_sees_the_task_finish(
    monkeypatch,
):
    """The live incident, replayed: throttled mid-poll, then the task completes.

    Red before the fix: the 401 became a Time4VPSAPIError out of `wait_for_task`,
    the reinstall was reported failed, and the only carrier of the new root
    password — the completed task's results — was never read.
    """
    from shared.clients import time4vps as module

    clock = _FakeAsyncio()
    monkeypatch.setattr(module, "asyncio", clock)
    stub = _SequenceAsyncClient(
        [
            _task(completed=None),
            _error(401, _RATE_LIMITED),
            _task(completed="2026-08-06 10:01:40", results=_REINSTALL_RESULTS),
        ]
    )
    client = Time4VPSClient("user", "secret")

    with patch("shared.clients.time4vps.httpx.AsyncClient", stub):
        task = await client.wait_for_task(275301, 4948782, timeout=600, poll_interval=10)

    assert task.completed == "2026-08-06 10:01:40"
    # The password is still recoverable from the task the poll waited for.
    assert client.extract_password(task.results) == _NEW_PASSWORD
    # 10s for the pending poll, then exactly the interval the provider asked for.
    assert clock.slept == [10, 24]
    assert len(stub.calls) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rate_limit_extends_the_poll_but_cannot_outlive_the_timeout(monkeypatch):
    """A provider stuck on throttling ends in TimeoutError, not an endless wait."""
    from shared.clients import time4vps as module

    clock = _FakeAsyncio()
    monkeypatch.setattr(module, "asyncio", clock)
    stub = _SequenceAsyncClient([_error(401, _RATE_LIMITED) for _ in range(5)])
    client = Time4VPSClient("user", "secret")

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", stub),
        pytest.raises(TimeoutError, match="4948782"),
    ):
        await client.wait_for_task(275301, 4948782, timeout=10, poll_interval=10)

    # The 24s the provider asked for is clipped to what is left of the budget.
    assert clock.slept == [10]
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_real_unauthorized_still_burns_the_attempt(monkeypatch):
    """401 without the rate-limit key stays fatal: one poll, error out."""
    from shared.clients import time4vps as module

    clock = _FakeAsyncio()
    monkeypatch.setattr(module, "asyncio", clock)
    stub = _SequenceAsyncClient([_error(401, _IP_NOT_ALLOWED), _task(completed="whenever")])
    client = Time4VPSClient("user", "secret")

    with (
        patch("shared.clients.time4vps.httpx.AsyncClient", stub),
        pytest.raises(Time4VPSAPIError) as excinfo,
    ):
        await client.wait_for_task(275301, 4948782, timeout=600, poll_interval=10)

    assert excinfo.value.body == _IP_NOT_ALLOWED
    assert len(stub.calls) == 1
    assert clock.slept == []


@pytest.mark.asyncio
async def test_password_reset_wait_survives_the_same_rate_limit(monkeypatch):
    """The explicit-reset fallback polls the same endpoint, so it is covered too."""
    from shared.clients import time4vps as module

    clock = _FakeAsyncio()
    monkeypatch.setattr(module, "asyncio", clock)
    stub = _SequenceAsyncClient(
        [
            _error(401, _RATE_LIMITED),
            _task(completed="2026-08-06 10:05:00", results=_REINSTALL_RESULTS),
        ]
    )
    client = Time4VPSClient("user", "secret")

    with patch("shared.clients.time4vps.httpx.AsyncClient", stub):
        password = await client.wait_for_password_reset(275301, 4948783, timeout=300)

    assert password == _NEW_PASSWORD
    assert clock.slept == [24]


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
