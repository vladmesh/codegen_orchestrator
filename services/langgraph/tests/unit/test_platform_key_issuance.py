"""Platform credentials resolved against a fake auth admin API, without a service branch."""

import base64
import json
import re
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr
import pytest
from structlog.testing import capture_logs
import yaml

from shared.contracts.queues.deploy import DeployOutcome
from shared.crypto import encrypt_dict
from src.clients.platform_auth import PlatformAuthAdminClient
from src.subgraphs.devops.secret_resolver import SecretResolverNode, TypedSecretResolutionError
from tests.unit.test_typed_env_resolution import _state


def _entries():
    # A fictional catalog package's YAML is all the resolver learns about this service.
    return yaml.safe_load("""
KEY:
  source: platform_key
  environments: [production]
  required: true
  service: geo-lookup
  scopes: [geo-lookup:read]
  quota: {lookups_per_day: 100}
URL:
  source: platform_base_url
  environments: [production]
  required: true
  service: geo-lookup
  url: https://geo.example.invalid
""")


class FakeAdmin:
    def __init__(self):
        self.product = None
        self.grants = {}
        self.keys = {}
        self.products = {}
        self.key_owners = {}
        self.events = []
        self.persisted = {}
        self.fail = None
        self.revoke_on_register = False

    async def persist(self, project_id, values):
        self.events.append("persist")
        self.persisted.update(values)

    def request(self, request):
        self.events.append(request.method + " " + request.url.path)
        if request.headers.get("Authorization") != "Bearer admin-private-value":
            return httpx.Response(401)
        if self.fail is not None:
            if isinstance(self.fail, Exception):
                raise self.fail
            return httpx.Response(self.fail, text="admin-private-value " + str(self.persisted))
        product_id = request.url.path.split("/")[4]
        product = self.products.get(product_id)
        if request.method == "GET":
            if product is None:
                return httpx.Response(404)
            return httpx.Response(200, json={**product, "keys": list(product["keys"].values())})
        body = json.loads(request.content)
        if "/keys/" in request.url.path:
            assert body["key"] in self.persisted.values(), "registration precedes storage"
            key_id = request.url.path.rsplit("/", 1)[1]
            if key_id in self.key_owners and self.key_owners[key_id] != product_id:
                return httpx.Response(409)
            keys = product["keys"]
            if key_id not in keys:
                keys[key_id] = {"key_id": key_id, "label": body["label"], "revoked_at": None}
                self.key_owners[key_id] = product_id
            if self.revoke_on_register:
                keys[key_id]["revoked_at"] = "2026-10-08T00:00:00Z"
                self.revoke_on_register = False
            return httpx.Response(200, json=keys[key_id])
        assert request.headers["If-None-Match"] == "*", "full replacement is forbidden"
        if "/grants/" in request.url.path:
            service = request.url.path.rsplit("/", 1)[1]
            if service in product["grants"]:
                return httpx.Response(412)
            product["grants"][service] = body
        else:
            if product is not None:
                return httpx.Response(412)
            product = {**body, "grants": {}, "keys": {}}
            self.products[product_id] = product
            if self.product is None:
                self.product = product
                self.grants = product["grants"]
                self.keys = product["keys"]
        return httpx.Response(201)


@pytest.fixture
def admin():
    return FakeAdmin()


@pytest.fixture
async def node(admin):
    transport = httpx.AsyncClient(transport=httpx.MockTransport(admin.request))
    client = PlatformAuthAdminClient(
        "https://auth.example.invalid", SecretStr("admin-private-value"), transport=transport
    )
    with patch("src.subgraphs.devops.secret_resolver.api_client") as api:
        api.merge_secrets = AsyncMock(side_effect=admin.persist)
        yield SecretResolverNode(platform_admin=client)
    await client.aclose()


async def test_fictional_package_issues_persists_registers_and_reuses_key(node, admin):
    with capture_logs() as logs:
        result = await node.run(_state(_entries()))
    key = result["secret_values"]["KEY"]
    assert re.fullmatch(r"cps_[a-z2-7]{12}_[A-Za-z0-9_-]{43}", key)
    assert len(base64.urlsafe_b64decode(key[17:] + "=")) == 32
    assert admin.persisted == {"KEY": key}
    assert key not in str(logs)
    assert "admin-private-value" not in str(logs)
    assert result["non_secret_values"] == {"URL": "https://geo.example.invalid"}
    assert result["missing_user_secrets"] == []
    assert result["resolution_outcome"] is None
    assert admin.grants == {
        "geo-lookup": {"scopes": ["geo-lookup:read"], "quota": {"lookups_per_day": 100}}
    }
    assert admin.events[0] == "persist"
    assert admin.product["orchestrator_project_id"] == "project-1"
    product_path = admin.events[1].split(" ", 1)[1]
    assert re.fullmatch(r"/admin/v1/products/[a-z0-9][a-z0-9-]{1,62}", product_path)
    admin.product["disabled"] = True
    admin.product["display_name"] = "Operator title"
    admin.grants["geo-lookup"]["quota"] = {"lookups_per_day": 7}
    admin.grants["geo-lookup"]["scopes"] = []
    admin.events.clear()
    state = _state(_entries(), secrets=encrypt_dict(admin.persisted))
    state["project_spec"].update(title="Renamed project", slug="renamed-project-0000")
    with patch.object(node, "_generate_platform_key", side_effect=AssertionError("regenerated")):
        second = await node.run(state)
    assert second == result
    assert "persist" not in admin.events
    assert admin.product["disabled"] is True
    assert admin.product["display_name"] == "Operator title"
    assert admin.events[0] == "GET " + product_path
    assert admin.grants["geo-lookup"] == {"scopes": [], "quota": {"lookups_per_day": 7}}
    assert len(admin.keys) == 1


async def test_revoked_stored_key_rotates_before_registration(node, admin):
    first = await node.run(_state(_entries()))
    old = first["secret_values"]["KEY"]
    admin.keys[old.split("_")[1]]["revoked_at"] = "2026-10-08T00:00:00Z"
    admin.events.clear()
    result = await node.run(_state(_entries(), secrets=encrypt_dict(admin.persisted)))
    new = result["secret_values"]["KEY"]
    assert new != old
    assert admin.persisted["KEY"] == new
    assert admin.events[0].startswith("GET ")
    assert admin.events[1] == "persist"
    assert len(admin.keys) == 2


async def test_key_revoked_between_read_and_registration_rotates(node, admin):
    first = await node.run(_state(_entries()))
    admin.revoke_on_register = True
    result = await node.run(_state(_entries(), secrets=encrypt_dict(admin.persisted)))
    assert result["secret_values"]["KEY"] != first["secret_values"]["KEY"]
    assert admin.persisted == result["secret_values"]


@pytest.mark.parametrize(
    "failure", [200, 302, 401, 403, 409, 422, 429, 500, 503, "timeout", "unreachable"]
)
async def test_admin_failures_are_typed_and_credential_safe(node, admin, failure):
    key = (await node.run(_state(_entries())))["secret_values"]["KEY"]
    admin.fail = (
        httpx.ReadTimeout(key + " admin-private-value")
        if failure == "timeout"
        else httpx.ConnectError(key + " admin-private-value")
        if failure == "unreachable"
        else failure
    )
    with capture_logs() as logs, pytest.raises(TypedSecretResolutionError) as caught:
        await node.run(_state(_entries(), secrets=encrypt_dict(admin.persisted)))
    assert caught.value.outcome is (
        DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
        if failure in (200, 302, 401, 403, 409, 422)
        else DeployOutcome.RETRY
    )
    visible = str(logs) + str(caught.value) + repr(caught.value) + repr(node)
    assert key not in visible
    assert "admin-private-value" not in visible
    assert caught.value.__cause__ is None


async def test_retry_after_failed_registration_reuses_durable_key(node, admin):
    admin.fail = 503
    with pytest.raises(TypedSecretResolutionError) as caught:
        await node.run(_state(_entries()))
    assert caught.value.outcome is DeployOutcome.RETRY
    key = admin.persisted["KEY"]
    admin.fail = None
    with patch.object(node, "_generate_platform_key", side_effect=AssertionError("regenerated")):
        result = await node.run(_state(_entries(), secrets=encrypt_dict(admin.persisted)))
    assert result["secret_values"]["KEY"] == key
    assert admin.keys[key.split("_")[1]]["revoked_at"] is None


async def test_admin_write_rechecks_fence_after_persistence(node, admin):
    from src.deploy_fence import DeployFenceLost

    state = _state(_entries())

    async def persist_then_lose(project_id, values):
        await admin.persist(project_id, values)
        await state["deploy_fence"].redis.delete(state["deploy_fence"].lock_key)

    with patch("src.subgraphs.devops.secret_resolver.api_client") as api:
        api.merge_secrets = AsyncMock(side_effect=persist_then_lose)
        with pytest.raises(DeployFenceLost):
            await node.run(state)
    assert admin.events == ["persist"]


async def test_storage_failure_registers_nothing(node, admin):
    with patch.object(node, "_save_secrets_to_project", side_effect=RuntimeError("private")):
        with pytest.raises(TypedSecretResolutionError, match="persist"):
            await node.run(_state(_entries()))
    assert admin.events == []


async def test_lost_fence_registers_nothing(node, admin):
    from src.deploy_fence import DeployFenceLost

    state = _state(_entries())
    await state["deploy_fence"].redis.delete(state["deploy_fence"].lock_key)
    with pytest.raises(DeployFenceLost):
        await node.run(state)
    assert admin.events == []


async def test_base_url_resolves_without_admin_configuration():
    result = await SecretResolverNode().run(_state({"URL": _entries()["URL"]}))
    assert result["non_secret_values"] == {"URL": "https://geo.example.invalid"}


async def test_production_resolver_reads_admin_settings_and_closes_client(admin, monkeypatch):
    from src.config.settings import Settings

    settings = Settings(
        _env_file=None,
        platform_auth_admin_url="https://auth.example.invalid",
        platform_auth_admin_token=SecretStr("admin-private-value"),
    )
    monkeypatch.setattr("src.subgraphs.devops.secret_resolver.get_settings", lambda: settings)
    transport = httpx.AsyncClient(transport=httpx.MockTransport(admin.request))
    with (
        patch("src.clients.platform_auth.httpx.AsyncClient", return_value=transport),
        patch("src.subgraphs.devops.secret_resolver.api_client") as api,
    ):
        api.merge_secrets = AsyncMock(side_effect=admin.persist)
        result = await SecretResolverNode().run(_state(_entries()))
    assert result["secret_values"] == admin.persisted
    assert result["missing_user_secrets"] == []
    assert transport.is_closed


@pytest.mark.parametrize("missing", ["platform_auth_admin_url", "platform_auth_admin_token"])
async def test_missing_admin_configuration_never_asks_user(monkeypatch, missing):
    from src.config.settings import Settings

    settings = Settings(
        _env_file=None,
        platform_auth_admin_url="https://auth.example.invalid",
        platform_auth_admin_token=SecretStr("admin-private-value"),
    )
    setattr(settings, missing, None)
    monkeypatch.setattr("src.subgraphs.devops.secret_resolver.get_settings", lambda: settings)
    with pytest.raises(TypedSecretResolutionError, match="platform_service_unconfigured") as caught:
        await SecretResolverNode().run(_state(_entries()))
    assert caught.value.outcome is DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED
    assert "admin-private-value" not in repr(settings)
