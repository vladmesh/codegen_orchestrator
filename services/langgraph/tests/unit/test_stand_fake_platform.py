"""Offline stand platform proof through the resolver and the HTTP boundary."""

import asyncio
from datetime import datetime
import hashlib
import json
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from shared.crypto import encrypt_dict
from shared.stand_fake_platform import create_app
from src.clients.platform_auth import PlatformAuthAdminClient
from src.config.settings import Settings
from src.subgraphs.devops.secret_resolver import SecretResolverNode
from tests.unit.test_platform_key_issuance import FakeAdmin, _entries
from tests.unit.test_typed_env_resolution import _state


@pytest.fixture
def app():
    return create_app(
        "private-admin",
        {
            "routes": [
                {
                    "method": "GET",
                    "path": "/geo-lookup/places/{place}",
                    "status": 200,
                    "body": {"name": "Fictional place", "observed_at": "{now}"},
                },
                {
                    "method": "GET",
                    "path": "/geo-lookup/unavailable",
                    "status": 503,
                    "body": {"error": "fixture unavailable"},
                },
            ]
        },
    )


@pytest.mark.parametrize("implementation", ["issuance-unit-fake", "stand-asgi"])
async def test_shared_admin_contract(implementation, app):
    key = "cps_abcdefghijkl_" + "x" * 43
    fake = FakeAdmin()
    fake.persisted["KEY"] = key
    transport = (
        httpx.MockTransport(fake.request)
        if implementation == "issuance-unit-fake"
        else httpx.ASGITransport(app=app)
    )
    headers = {
        "Authorization": "Bearer "
        + ("admin-private-value" if implementation == "issuance-unit-fake" else "private-admin"),
        "If-None-Match": "*",
    }
    product = {"display_name": "Original", "orchestrator_project_id": "p1", "disabled": False}
    grant = {"scopes": ["geo-lookup:read"], "quota": {"daily": 3}}
    key_body = {"key": key, "label": "original"}
    base = "/admin/v1/products"
    # The same wire table runs against both independent implementations.
    cases = [
        ("GET", "/first", None, 404),
        ("PUT", "/first", product, 201),
        ("PUT", "/first", {**product, "disabled": True}, 412),
        ("PUT", "/first/grants/geo-lookup", grant, 201),
        ("PUT", "/first/grants/geo-lookup", {"scopes": [], "quota": {}}, 412),
        ("PUT", "/first/keys/abcdefghijkl", key_body, 200),
        ("PUT", "/first/keys/abcdefghijkl", {**key_body, "label": "replacement"}, 200),
        ("PUT", "/second", product, 201),
        ("PUT", "/second/keys/abcdefghijkl", key_body, 409),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as http:
        for method, suffix, body, status in cases:
            response = await http.request(method, base + suffix, json=body, headers=headers)
            assert response.status_code == status, (method, suffix, response.text)
        read = (await http.get(base + "/first", headers=headers)).json()
        assert read["disabled"] is False
        assert read["display_name"] == "Original"
        assert read["grants"] == {"geo-lookup": grant}
        assert read["keys"] == [{"key_id": "abcdefghijkl", "label": "original", "revoked_at": None}]
        state = fake.products if implementation == "issuance-unit-fake" else app.state.products
        state["first"]["keys"]["abcdefghijkl"]["revoked_at"] = "2026-10-08T00:00:00Z"
        response = await http.put(base + "/first/keys/abcdefghijkl", json=key_body, headers=headers)
        assert response.json()["revoked_at"] == "2026-10-08T00:00:00Z"
        assert (await http.get(base + "/first", headers=headers)).json()["keys"][0][
            "revoked_at"
        ] == "2026-10-08T00:00:00Z"
        for method in ("GET", "PUT"):
            assert (
                await http.request(
                    method, base + "/first", json=product, headers={"Authorization": "Bearer wrong"}
                )
            ).status_code == 401
        assert (await http.get(base + "/first", headers=headers)).json()["disabled"] is False


async def test_resolver_issues_service_credential_and_rotation_revokes_access(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"
    ) as http:
        admin = PlatformAuthAdminClient("http://fake", SecretStr("private-admin"), transport=http)
        persisted = {}

        async def persist(project_id, values):
            persisted.update(values)

        with patch("src.subgraphs.devops.secret_resolver.api_client") as api:
            api.merge_secrets = AsyncMock(side_effect=persist)
            node = SecretResolverNode(platform_admin=admin)
            result = await node.run(_state(_entries()))
            key = result["secret_values"]["KEY"]
            assert persisted == {"KEY": key}
            headers = {"Authorization": "Bearer " + key}
            response = await http.get("/geo-lookup/places/forest", headers=headers)
            assert response.status_code == 200
            assert response.json()["name"] == "Fictional place"
            assert datetime.fromisoformat(response.json()["observed_at"]).tzinfo is not None
            assert (await http.get("/geo-lookup/unavailable", headers=headers)).status_code == 503
            assert (
                await http.get("/other-service/places/forest", headers=headers)
            ).status_code == 403
            assert (await http.get("/geo-lookup/places/forest")).status_code == 401
            assert (
                await http.get(
                    "/geo-lookup/places/forest", headers={"Authorization": "Bearer unknown"}
                )
            ).status_code == 401
            product_id = "orch-" + hashlib.sha256(b"project-1").hexdigest()[:58]
            app.state.products[product_id]["keys"][key.split("_")[1]]["revoked_at"] = (
                "2026-10-08T00:00:00Z"
            )
            assert (await http.get("/geo-lookup/places/forest", headers=headers)).status_code == 401
            rotated = await node.run(_state(_entries(), secrets=encrypt_dict(persisted)))
            assert rotated["secret_values"]["KEY"] != key
            app.state.products[product_id]["disabled"] = True
            assert (
                await http.get(
                    "/geo-lookup/places/forest",
                    headers={"Authorization": "Bearer " + rotated["secret_values"]["KEY"]},
                )
            ).status_code == 403


async def test_base_url_override_is_explicitly_stand_only(monkeypatch):
    settings = Settings(
        _env_file=None,
        live_contour="stand",
        platform_base_url_override="https://stand.example.invalid/platform-fake/{service}",
    )
    monkeypatch.setattr("src.subgraphs.devops.secret_resolver.get_settings", lambda: settings)
    result = await SecretResolverNode().run(_state({"URL": _entries()["URL"]}))
    assert (
        result["non_secret_values"]["URL"]
        == "https://stand.example.invalid/platform-fake/geo-lookup"
    )


@pytest.mark.parametrize("contour", ["production", "dev", None])
def test_non_stand_refuses_override(contour):
    with pytest.raises(ValidationError, match="stand"):
        Settings(
            _env_file=None,
            live_contour=contour,
            platform_base_url_override="https://stand.example.invalid/{service}",
        )


@pytest.mark.parametrize(
    "template",
    [
        "http://stand/{service}",
        "https://stand/no-service",
        "https://stand/{unknown}",
        "https://{service}.example.invalid/",
        "https://user:password@stand/{service}",
    ],
)
def test_override_requires_https_path_template(template):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, live_contour="stand", platform_base_url_override=template)


async def test_runtime_loads_fixture_from_environment(tmp_path, monkeypatch):
    from shared.stand_fake_platform.__main__ import main

    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"routes": []}))
    monkeypatch.setenv("STAND_PLATFORM_FIXTURE_PATH", str(fixture))
    monkeypatch.setenv("STAND_PLATFORM_ADMIN_TOKEN", "stand-private-token")
    with (
        patch("shared.stand_fake_platform.__main__.uvicorn.run") as run,
        patch("shared.stand_fake_platform.__main__.setup_logging"),
    ):
        main()
    app = run.call_args.args[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"
    ) as http:
        assert (
            await http.get(
                "/admin/v1/products/missing",
                headers={"Authorization": "Bearer stand-private-token"},
            )
        ).status_code == 404


@pytest.mark.parametrize("token", [None, ""])
def test_runtime_refuses_missing_admin_token(tmp_path, monkeypatch, token):
    from shared.stand_fake_platform.__main__ import main

    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"routes": []}))
    monkeypatch.setenv("STAND_PLATFORM_FIXTURE_PATH", str(fixture))
    monkeypatch.delenv("STAND_PLATFORM_ADMIN_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("STAND_PLATFORM_ADMIN_TOKEN", token)
    with (
        patch("shared.stand_fake_platform.__main__.setup_logging"),
        pytest.raises((KeyError, ValueError), match="STAND_PLATFORM_ADMIN_TOKEN"),
    ):
        main()


@pytest.mark.parametrize("suffix", ["", "/grants/geo-lookup"])
async def test_concurrent_create_only_put_preserves_the_winner(app, suffix):
    arrived = 0
    both_arrived = asyncio.Event()
    bodies = [
        {"display_name": name, "orchestrator_project_id": "p", "disabled": False}
        for name in ("first", "second")
    ]
    if suffix:
        app.state.products["one"] = {**bodies[0], "grants": {}, "keys": {}}
        bodies = [{"scopes": [name], "quota": {}} for name in ("first", "second")]

    class HeldBody(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await both_arrived.wait()
            yield json.dumps(self.body).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"
    ) as http:
        responses = await asyncio.gather(
            *[
                http.put(
                    "/admin/v1/products/one" + suffix,
                    content=HeldBody(body),
                    headers={"Authorization": "Bearer private-admin", "If-None-Match": "*"},
                )
                for body in bodies
            ]
        )
    assert sorted(response.status_code for response in responses) == [201, 412]
    winning_body = bodies[
        next(i for i, response in enumerate(responses) if response.status_code == 201)
    ]
    product = app.state.products["one"]
    assert (
        product["grants"]["geo-lookup"] if suffix else {k: product[k] for k in winning_body}
    ) == winning_body
