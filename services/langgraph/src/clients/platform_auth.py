"""Create-only platform auth administration. Errors never retain credential-bearing bodies."""

from datetime import datetime
from http import HTTPStatus
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr

from shared.contracts.env_contract import PlatformKeyEntry
from shared.contracts.queues.deploy import DeployOutcome

from ..deploy_fence import DeployFence, DeployWrite


class PlatformAuthError(RuntimeError):
    """A bounded diagnostic and an existing deploy disposition."""

    def __init__(self, outcome: DeployOutcome, diagnostic: str):
        super().__init__(diagnostic)
        self.outcome = outcome


class ProductInput(BaseModel):
    display_name: str
    orchestrator_project_id: str
    disabled: bool = False


class GrantInput(BaseModel):
    scopes: list[str]
    quota: dict[str, int]


class KeyInput(BaseModel):
    key: SecretStr
    label: str


class RegisteredKey(BaseModel):
    key_id: str = Field(pattern=r"^[a-z2-7]{12}$")
    revoked_at: datetime | None


class ProductKeys(BaseModel):
    keys: list[RegisteredKey]


class PlatformAdmin(Protocol):
    """The only admin operations the deploy resolver needs."""

    async def product_keys(self, product_id: str) -> list[RegisteredKey]: ...

    async def create_product(
        self, product_id: str, body: ProductInput, fence: DeployFence
    ) -> None: ...

    async def create_grant(
        self, product_id: str, entry: PlatformKeyEntry, fence: DeployFence
    ) -> None: ...

    async def register_key(
        self, product_id: str, key_id: str, body: KeyInput, fence: DeployFence
    ) -> RegisteredKey: ...


class PlatformAuthAdminClient:
    """A bounded httpx client. Product/grant replacement is never exposed."""

    def __init__(
        self, base_url: str, token: SecretStr, *, transport: httpx.AsyncClient | None = None
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise PlatformAuthError(
                DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, "platform_auth_configuration_invalid"
            )
        self._base_url = base_url.rstrip("/") + "/admin/v1/products"
        self._token = token
        self._transport = transport or httpx.AsyncClient(timeout=15.0, follow_redirects=False)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def _call_admin(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        create_only: bool = False,
        fence: DeployFence | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": "Bearer " + self._token.get_secret_value()}
        if create_only:
            headers["If-None-Match"] = "*"
        if fence is not None:
            await fence.ensure_held(DeployWrite.PRODUCT_ACCESS)
        try:
            response = await self._transport.request(
                method, self._base_url + path, json=body, headers=headers
            )
        except httpx.RequestError:
            raise PlatformAuthError(DeployOutcome.RETRY, "platform_auth_unavailable") from None
        status = response.status_code
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR or status == HTTPStatus.TOO_MANY_REQUESTS:
            raise PlatformAuthError(DeployOutcome.RETRY, "platform_auth_unavailable")
        if status in {401, 403}:
            raise PlatformAuthError(
                DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, "platform_auth_unauthorized"
            )
        accepted = {200, 201} | ({412} if create_only else {404} if method == "GET" else set())
        if status not in accepted:
            raise PlatformAuthError(
                DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, "platform_auth_request_refused"
            )
        return response

    @staticmethod
    def _validated_response[T: BaseModel](response: httpx.Response, model: type[T]) -> T:
        try:
            return model.model_validate(response.json())
        except ValueError:
            raise PlatformAuthError(
                DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, "platform_auth_response_invalid"
            ) from None

    async def product_keys(self, product_id: str) -> list[RegisteredKey]:
        response = await self._call_admin("GET", f"/{product_id}")
        if response.status_code == HTTPStatus.NOT_FOUND:
            return []
        return self._validated_response(response, ProductKeys).keys

    async def create_product(self, product_id: str, body: ProductInput, fence: DeployFence) -> None:
        await self._call_admin(
            "PUT", f"/{product_id}", body=body.model_dump(), create_only=True, fence=fence
        )

    async def create_grant(
        self, product_id: str, entry: PlatformKeyEntry, fence: DeployFence
    ) -> None:
        body = GrantInput(scopes=entry.scopes, quota=entry.quota)
        await self._call_admin(
            "PUT",
            f"/{product_id}/grants/{entry.service}",
            body=body.model_dump(),
            create_only=True,
            fence=fence,
        )

    async def register_key(
        self, product_id: str, key_id: str, body: KeyInput, fence: DeployFence
    ) -> RegisteredKey:
        response = await self._call_admin(
            "PUT",
            f"/{product_id}/keys/{key_id}",
            body={**body.model_dump(exclude={"key"}), "key": body.key.get_secret_value()},
            fence=fence,
        )
        registered = self._validated_response(response, RegisteredKey)
        if registered.key_id != key_id:
            raise PlatformAuthError(
                DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, "platform_auth_response_invalid"
            )
        return registered
