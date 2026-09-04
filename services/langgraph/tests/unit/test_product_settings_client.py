"""Credential-safe write-and-prove client for the generated core settings contract."""

from unittest.mock import AsyncMock

import httpx
import pytest

from shared.contracts.dto.product_brief import InitialSetting, SettingScope
from shared.contracts.dto.settings_seed import (
    CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL,
    CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL,
    SettingsSeedFailureKind,
)
from src.clients.product_settings import GeneratedServiceSettingsClient

_SET = httpx.Request("POST", "https://service/settings/set")
_GET = httpx.Request("POST", "https://service/settings/get")


def _value(key="reminders.default_hour", value=9, **overrides) -> InitialSetting:
    return InitialSetting(key=key, value=value, **overrides)


def _readback(key="reminders.default_hour", value=9, scope="product", subject_id=None):
    return httpx.Response(
        200,
        json={
            "contract_version": 1,
            "key": key,
            "scope": scope,
            "subject_id": subject_id,
            "value": value,
        },
        request=_GET,
    )


@pytest.mark.asyncio
async def test_capability_is_a_header_and_the_written_value_is_proved_by_readback():
    transport = AsyncMock()
    transport.request.side_effect = [
        httpx.Response(200, json={"contract_version": 1}, request=_SET),
        _readback(),
    ]

    proofs = await GeneratedServiceSettingsClient(
        "https://service", transport=transport
    ).seed_and_resolve([_value()], capability="not-in-a-url")

    assert [p.written for p in proofs] == [True]
    write, read = transport.request.await_args_list
    assert write.args == ("POST", "https://service/settings/set")
    assert write.kwargs["headers"] == {"X-Settings-Capability": "not-in-a-url"}
    assert write.kwargs["json"] == {
        "contract_version": 1,
        "key": "reminders.default_hour",
        "scope": "product",
        "subject_id": None,
        "value": 9,
    }
    assert "not-in-a-url" not in str(write.args)
    assert "not-in-a-url" not in str(write.kwargs["json"])
    # A read never carries the deployment capability.
    assert read.args == ("POST", "https://service/settings/get")
    assert "headers" not in read.kwargs
    assert "value" not in read.kwargs["json"]


@pytest.mark.asyncio
async def test_a_user_scoped_value_names_its_subject_on_both_calls():
    transport = AsyncMock()
    transport.request.side_effect = [
        httpx.Response(200, json={"contract_version": 1}, request=_SET),
        _readback(key="reminders.locale", value="ru", scope="user", subject_id=7),
    ]

    setting = _value(key="reminders.locale", value="ru", scope=SettingScope.USER, subject_id=7)
    proofs = await GeneratedServiceSettingsClient(
        "https://service", transport=transport
    ).seed_and_resolve([setting], capability="c")

    assert [p.written for p in proofs] == [True]
    for call in transport.request.await_args_list:
        assert call.kwargs["json"]["scope"] == "user"
        assert call.kwargs["json"]["subject_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "failure"),
    [
        (
            [
                httpx.Response(
                    404,
                    json={"detail": CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL},
                    request=_SET,
                )
            ],
            SettingsSeedFailureKind.KEY_NOT_DECLARED,
        ),
        (
            [httpx.Response(404, json={"detail": "Not Found"}, request=_SET)],
            SettingsSeedFailureKind.SET_REJECTED,
        ),
        (
            [
                httpx.Response(
                    422,
                    json={"detail": CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL},
                    request=_SET,
                )
            ],
            SettingsSeedFailureKind.VALUE_REJECTED,
        ),
        (
            [httpx.Response(422, json={"detail": [{"msg": "not a string"}]}, request=_SET)],
            SettingsSeedFailureKind.SET_REJECTED,
        ),
        (
            [httpx.Response(401, request=_SET)],
            SettingsSeedFailureKind.SET_REJECTED,
        ),
        (
            [httpx.Response(200, json={}, request=_SET), httpx.Response(503, request=_GET)],
            SettingsSeedFailureKind.READBACK_REJECTED,
        ),
        (
            [
                httpx.Response(200, json={}, request=_SET),
                httpx.Response(200, content=b"not json", request=_GET),
            ],
            SettingsSeedFailureKind.MALFORMED_READBACK,
        ),
        (
            [
                httpx.Response(200, json={}, request=_SET),
                _readback(key="another.key"),
            ],
            SettingsSeedFailureKind.MALFORMED_READBACK,
        ),
        (
            [httpx.Response(200, json={}, request=_SET), _readback(value=8)],
            SettingsSeedFailureKind.READBACK_MISMATCH,
        ),
    ],
)
async def test_every_refusal_is_a_bounded_kind(responses, failure):
    transport = AsyncMock()
    transport.request.side_effect = responses

    proofs = await GeneratedServiceSettingsClient(
        "https://service", transport=transport
    ).seed_and_resolve([_value()], capability="c")

    assert proofs == [proofs[0].__class__(written=False, failure=failure)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_call", [0, 1])
async def test_an_unreachable_product_is_transport_on_either_call(failing_call):
    transport = AsyncMock()
    responses = [httpx.Response(200, json={}, request=_SET), _readback()]
    responses[failing_call] = httpx.ConnectError("boom")
    transport.request.side_effect = responses

    proofs = await GeneratedServiceSettingsClient(
        "https://service", transport=transport
    ).seed_and_resolve([_value()], capability="c")

    assert proofs[0].written is False
    assert proofs[0].failure is SettingsSeedFailureKind.TRANSPORT


@pytest.mark.asyncio
async def test_a_refused_setting_does_not_stop_the_ones_after_it():
    transport = AsyncMock()
    transport.request.side_effect = [
        httpx.Response(
            404,
            json={"detail": CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL},
            request=_SET,
        ),
        httpx.Response(200, json={}, request=_SET),
        _readback(key="reminders.locale", value="ru"),
    ]

    proofs = await GeneratedServiceSettingsClient(
        "https://service", transport=transport
    ).seed_and_resolve([_value(), _value(key="reminders.locale", value="ru")], capability="c")

    assert [p.written for p in proofs] == [False, True]
    assert proofs[0].failure is SettingsSeedFailureKind.KEY_NOT_DECLARED
