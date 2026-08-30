"""Credential-safe proof client for generated-service permanent access."""

from unittest.mock import AsyncMock

import httpx
import pytest

from src.clients.users_grant import GeneratedServiceGrantClient, GrantFailureKind


@pytest.mark.asyncio
async def test_grant_sends_capability_only_as_header_and_requires_active_readback():
    transport = AsyncMock()
    transport.request.side_effect = [
        httpx.Response(200, request=httpx.Request("POST", "https://service/users/grant")),
        httpx.Response(
            200,
            json={
                "user_id": 12,
                "status": "active",
                "channel": "telegram",
                "external_id": "84",
            },
            request=httpx.Request("GET", "https://service/users/access"),
        ),
    ]

    proof = await GeneratedServiceGrantClient(
        "https://service", transport=transport
    ).grant_and_resolve(channel="telegram", external_id="84", capability="not-in-a-url")

    assert proof.active is True
    grant = transport.request.await_args_list[0]
    assert grant.args == ("POST", "https://service/users/grant")
    assert grant.kwargs["headers"] == {"X-Grant-Capability": "not-in-a-url"}
    assert "not-in-a-url" not in str(grant.args)
    assert "not-in-a-url" not in str(grant.kwargs["json"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "failure"),
    [
        (
            [httpx.Response(403, request=httpx.Request("POST", "https://service/users/grant"))],
            GrantFailureKind.GRANT_REJECTED,
        ),
        (
            [
                httpx.Response(200, request=httpx.Request("POST", "https://service/users/grant")),
                httpx.Response(503, request=httpx.Request("GET", "https://service/users/access")),
            ],
            GrantFailureKind.ACCESS_REJECTED,
        ),
        (
            [
                httpx.Response(200, request=httpx.Request("POST", "https://service/users/grant")),
                httpx.Response(
                    200,
                    json={
                        "user_id": 12,
                        "status": "inactive",
                        "channel": "telegram",
                        "external_id": "84",
                    },
                    request=httpx.Request("GET", "https://service/users/access"),
                ),
            ],
            GrantFailureKind.INACTIVE,
        ),
        (
            [
                httpx.Response(200, request=httpx.Request("POST", "https://service/users/grant")),
                httpx.Response(
                    200,
                    json={"channel": "telegram", "external_id": "84"},
                    request=httpx.Request("GET", "https://service/users/access"),
                ),
            ],
            GrantFailureKind.MALFORMED_ACCESS,
        ),
        (
            [
                httpx.Response(200, request=httpx.Request("POST", "https://service/users/grant")),
                httpx.Response(
                    200,
                    json={
                        "user_id": 12,
                        "status": "unknown",
                        "channel": "telegram",
                        "external_id": "84",
                    },
                    request=httpx.Request("GET", "https://service/users/access"),
                ),
            ],
            GrantFailureKind.MALFORMED_ACCESS,
        ),
    ],
)
async def test_grant_client_returns_a_bounded_safe_failure(responses, failure):
    transport = AsyncMock()
    transport.request.side_effect = responses

    proof = await GeneratedServiceGrantClient(
        "https://service", transport=transport
    ).grant_and_resolve(channel="telegram", external_id="84", capability="never-report")

    assert proof.active is False
    assert proof.failure is failure


@pytest.mark.asyncio
async def test_grant_client_treats_transport_failure_as_safe_failure():
    transport = AsyncMock()
    transport.request.side_effect = httpx.ConnectError("unavailable")

    proof = await GeneratedServiceGrantClient(
        "https://service", transport=transport
    ).grant_and_resolve(channel="telegram", external_id="84", capability="never-report")

    assert proof.active is False
    assert proof.failure is GrantFailureKind.TRANSPORT
