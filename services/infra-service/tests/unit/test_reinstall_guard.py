from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.clients.time4vps import Time4VPSClient
from src.provisioner.operations import reinstall_and_provision

# The live 2026-08-06 refusal: a rate limit, not a lost authorization.
_RATE_LIMITED = '{"error":[["wait_x_between_action",24],"unauthorized"]}'
_NEW_PASSWORD = "Xk9mP3qR7"  # noqa: S105 — fixture value, not a credential
_REINSTALL_RESULTS = f"Password: \t<a onclick='this.innerHTML = \"{_NEW_PASSWORD}\"'>Reveal</a>"


@pytest.mark.asyncio
async def test_reinstall_refuses_server_outside_allowlist(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "2002")
    client = MagicMock()
    client.reinstall_server = AsyncMock()

    success, message = await reinstall_and_provision(
        time4vps_client=client,
        server_handle="vps-1001",
        provider="time4vps",
        is_managed=True,
        server_id=1001,
        server_ip="203.0.113.10",
        os_template="ubuntu",
        ssh_manager=MagicMock(),
        ansible_runner=MagicMock(),
    )

    assert success is False
    assert "not authorized" in message
    client.reinstall_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_reinstall_refuses_provider_id_with_changed_ip(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    client = MagicMock()
    client.get_server_details = AsyncMock(return_value=MagicMock(ip="203.0.113.99"))
    client.reinstall_server = AsyncMock()

    success, message = await reinstall_and_provision(
        time4vps_client=client,
        server_handle="vps-1001",
        provider="time4vps",
        is_managed=True,
        server_id=1001,
        server_ip="203.0.113.10",
        os_template="ubuntu",
        ssh_manager=MagicMock(),
        ansible_runner=MagicMock(),
    )

    assert success is False
    assert "Provider identity mismatch" in message
    client.reinstall_server.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_ip", [None, ""])
async def test_reinstall_refuses_missing_provider_ip(monkeypatch, provider_ip):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    client = MagicMock()
    client.get_server_details = AsyncMock(return_value=MagicMock(ip=provider_ip))
    client.reinstall_server = AsyncMock()

    success, message = await reinstall_and_provision(
        time4vps_client=client,
        server_handle="vps-1001",
        provider="time4vps",
        is_managed=True,
        server_id=1001,
        server_ip="203.0.113.10",
        os_template="ubuntu",
        ssh_manager=MagicMock(),
        ansible_runner=MagicMock(),
    )

    assert success is False
    assert "Provider identity mismatch" in message
    client.reinstall_server.assert_not_awaited()


class _ScriptedTransport:
    """httpx.AsyncClient stand-in replaying the provider's answers in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url))
        status, payload = self._responses.pop(0)
        request = httpx.Request(method, url)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, request=request)
        return httpx.Response(status, json=payload, request=request)


class _FakeAsyncio:
    """Fake clock so the boot wait and the rate-limit wait cost no real seconds."""

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


@pytest.mark.asyncio
async def test_rate_limited_poll_still_yields_the_new_root_password(monkeypatch):
    """A throttled poll must not cost us the reinstall's only password carrier.

    Red before the fix: `wait_for_task` raised on the 401, the broad handler in
    `reinstall_and_provision` returned "Reinstall failed", and `extract_password`
    was never reached even though the task completed on the provider side.
    """
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "275301")

    from shared.clients import time4vps as client_module
    from src.provisioner import operations as operations_module

    clock = _FakeAsyncio()
    monkeypatch.setattr(client_module, "asyncio", clock)
    monkeypatch.setattr(operations_module, "asyncio", clock)
    monkeypatch.setattr(operations_module, "notify_admins_best_effort", AsyncMock())
    monkeypatch.setattr(operations_module, "update_server_labels", AsyncMock())
    # The completion write is its own call now: the phase and the QA identity it
    # created are recorded together, by one function.
    monkeypatch.setattr(operations_module, "mark_provisioning_complete", AsyncMock())

    transport = _ScriptedTransport(
        [
            (200, {"ip": "203.0.113.10"}),  # identity re-check before the destructive call
            (200, {"task_id": 4948782}),  # POST /reinstall
            (200, {"name": "server_recreate", "activated": "09:56", "completed": None}),
            (401, _RATE_LIMITED),  # the observed throttle, mid-poll
            (
                200,
                {
                    "name": "server_recreate",
                    "activated": "09:56",
                    "completed": "10:01:40",
                    "results": _REINSTALL_RESULTS,
                },
            ),
        ]
    )
    ansible = MagicMock()
    ansible.run_playbook.return_value = (True, "ok")

    with patch("shared.clients.time4vps.httpx.AsyncClient", transport):
        success, message = await reinstall_and_provision(
            time4vps_client=Time4VPSClient("user", "secret"),
            server_handle="vps-275301",
            provider="time4vps",
            is_managed=True,
            server_id=275301,
            server_ip="203.0.113.10",
            os_template="kvm-ubuntu-24.04-gpt-x86_64",
            ssh_manager=MagicMock(),
            ansible_runner=ansible,
        )

    assert success is True, message
    # The password extracted from the completed task is what the access phase used.
    assert ansible.run_playbook.call_args_list[0].kwargs["root_password"] == _NEW_PASSWORD
    # No explicit reset was needed, so the reinstall result really was the source.
    assert [call[0] for call in transport.calls].count("POST") == 1
    assert 24 in clock.slept
