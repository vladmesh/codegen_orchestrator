"""The operator's authenticated view of the Codegen API, through its existing routes.

Everything here is an existing public API contract: the internal promo-code batch,
user readback, owner-scoped project reads, the operator-owned
`capabilities.module_rollout` system config, Story/Brief/Task/Run reads and the
owner's project teardown, all through the shared internal API transport. A
refusal keeps the route and status, never a response body: a body can echo a
code or a value.
"""

from __future__ import annotations

from typing import Any
import uuid

import httpx

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.capability_preview import CapabilityPlan, ModuleRollout
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import DeployRunResult, QARunResult

MODULE_ROLLOUT_KEY = "capabilities.module_rollout"
REQUEST_TIMEOUT_SECONDS = 30


class ApiRefused(RuntimeError):  # noqa: N818 - a refusal named by its route
    """One API call answered something other than what the operation needs."""

    def __init__(self, route: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"{route} answered {status if status is not None else 'nothing'}")
        self.route = route
        self.status = status
        self.detail = detail


class CodegenApi(InternalAPIClient):
    """The Codegen API's existing routes over the shared internal API transport.

    The transport sets the runtime's `INTERNAL_API_KEY` itself; a user-scoped read
    adds only `X-Telegram-ID`, the way the Telegram bot asks as a user.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, timeout=REQUEST_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self.close()

    async def _call(
        self,
        method: str,
        route: str,
        *,
        as_user: int | None = None,
        accept: frozenset[int] = frozenset({200}),
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"X-Telegram-ID": str(as_user)} if as_user is not None else {}
        try:
            response = await self.request_raw(method, route, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiRefused(self.api_path(route), None, type(exc).__name__) from None
        if response.status_code not in accept:
            raise ApiRefused(self.api_path(route), response.status_code)
        return response

    # --- registration ---------------------------------------------------------

    async def user_by_telegram(self, telegram_id: int) -> dict | None:
        response = await self._call(
            "GET", f"users/by-telegram/{telegram_id}", accept=frozenset({200, 404})
        )
        return None if response.status_code == httpx.codes.NOT_FOUND else response.json()

    async def mint_promo(self, *, credits_microusd: int, reservation_microusd: int) -> dict:
        """One code; the caller adds `code` to the redaction set before anything else."""
        response = await self._call(
            "POST",
            "promo-codes/batch",
            accept=frozenset({201}),
            json={
                "quantity": 1,
                "credits_microusd": credits_microusd,
                "attempt_reservation_microusd": reservation_microusd,
            },
        )
        batch = response.json()
        if not isinstance(batch, list) or len(batch) != 1:
            raise ApiRefused("/api/promo-codes/batch", response.status_code, "not one code")
        return batch[0]

    # --- projects and the module rollout --------------------------------------

    async def owned_projects(self, telegram_id: int) -> list[dict]:
        """The projects the API scopes to this user when it asks as them."""
        return (await self._call("GET", "projects/", as_user=telegram_id)).json()

    async def project(self, project_id: str, *, as_user: int | None = None) -> dict:
        return (await self._call("GET", f"projects/{project_id}", as_user=as_user)).json()

    async def module_rollout(self) -> dict:
        """The rollout config's whole value: unrelated keys are preserved by the writer."""
        value = (await self._call("GET", f"system-configs/{MODULE_ROLLOUT_KEY}")).json()["value"]
        if not isinstance(value, dict):
            raise ApiRefused(
                self.api_path(f"system-configs/{MODULE_ROLLOUT_KEY}"), 200, "not an object"
            )
        ModuleRollout.model_validate({"project_ids": value.get("project_ids", [])})
        return value

    async def write_module_rollout(self, value: dict) -> dict:
        response = await self._call(
            "PATCH",
            f"system-configs/{MODULE_ROLLOUT_KEY}",
            json={"value": value, "updated_by": "synthetic-buyer"},
        )
        return response.json()["value"]

    # --- the order's native work ----------------------------------------------

    async def stories(self, project_id: str) -> list[dict]:
        return (await self._call("GET", "stories/", params={"project_id": project_id})).json()

    async def story(self, story_id: str) -> dict:
        return (await self._call("GET", f"stories/{story_id}")).json()

    async def brief_by_story(self, story_id: str) -> dict | None:
        response = await self._call(
            "GET", f"product-briefs/by-story/{story_id}", accept=frozenset({200, 404})
        )
        return None if response.status_code == httpx.codes.NOT_FOUND else response.json()

    async def brief(self, brief_id: str) -> dict:
        return (await self._call("GET", f"product-briefs/{brief_id}")).json()

    async def repositories(self, project_id: str) -> list[dict]:
        return (await self._call("GET", "repositories/", params={"project_id": project_id})).json()

    async def capability_plan(self, brief_id: str) -> CapabilityPlan | None:
        response = await self._call(
            "GET",
            f"product-briefs/{brief_id}/capability-plan",
            accept=frozenset({200, 404}),
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        return CapabilityPlan.model_validate(response.json())

    async def capability_preview(self, preview_id: str) -> dict:
        return (await self._call("GET", f"capability-previews/{preview_id}")).json()

    async def tasks(self, story_id: str) -> list[dict]:
        return (
            await self._call("GET", "tasks/", params={"story_id": story_id, "sort": "created_at"})
        ).json()

    async def runs(
        self,
        *,
        story_id: str | None = None,
        run_type: RunType | None = None,
        status: str | None = None,
    ) -> list[dict]:
        params: dict[str, str] = {}
        if story_id is not None:
            params["story_id"] = story_id
        if run_type is not None:
            params["run_type"] = run_type.value
        if status is not None:
            params["status"] = status
        return (await self._call("GET", "runs/", params=params)).json()

    async def bot_liveness(self, project_id: str) -> dict:
        """Internal-only: the API asks Telegram with the token it holds, as the service."""
        return (await self._call("GET", f"projects/{project_id}/telegram/liveness")).json()

    # --- teardown -------------------------------------------------------------

    async def request_teardown(self, project_id: str, telegram_id: int) -> dict:
        return (
            await self._call("POST", f"projects/{project_id}/teardown", as_user=telegram_id)
        ).json()

    async def teardown_state(self, project_id: str, telegram_id: int) -> dict:
        return (
            await self._call("GET", f"projects/{project_id}/teardown", as_user=telegram_id)
        ).json()

    async def delete_project(self, project_id: str, telegram_id: int) -> int:
        response = await self._call(
            "DELETE", f"projects/{project_id}", as_user=telegram_id, accept=frozenset({204})
        )
        return response.status_code

    async def deletion_confirmed(self, project_id: str, telegram_id: int) -> bool:
        response = await self._call(
            "GET", f"projects/{project_id}", as_user=telegram_id, accept=frozenset({200, 404})
        )
        return response.status_code == httpx.codes.NOT_FOUND


def typed_deploy_result(run: dict) -> DeployRunResult | None:
    """A deploy Run's typed result, or None while it has none."""
    result = run.get("result")
    return None if result is None else DeployRunResult.model_validate(result)


def typed_qa_result(run: dict) -> QARunResult | None:
    result = run.get("result")
    return None if result is None else QARunResult.model_validate(result)


def project_ids(rollout: dict) -> list[str]:
    return [str(uuid.UUID(str(value))) for value in rollout.get("project_ids", [])]
