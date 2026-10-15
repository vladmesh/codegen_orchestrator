"""Service-specific API client for Telegram bot."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.user import UserDTO
from shared.diagnostics import safe_validation_errors
from src.config import get_settings

# A user is waiting on the other side of every one of these calls, so the bot
# gives the API a third of the time the background services do.
TELEGRAM_TIMEOUT_SECONDS = 10.0


class ProjectDisplayConfig(BaseModel):
    """The keys of a project's free-form `config` the bot displays; each may be absent."""

    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    repo_url: str | None = None
    deployed_url: str | None = None


class ProjectDetails(ProjectDTO):
    """A project as its details view reads it, with the displayed config keys typed."""

    config: ProjectDisplayConfig = ProjectDisplayConfig()


class APIResponseError(ValueError):
    """An API response does not match the model the bot reads it as."""

    def __init__(self, path: str, error: ValidationError) -> None:
        super().__init__(
            f"API response for {path} is not a {error.title}: {safe_validation_errors(error)}"
        )
        self.path = path


_PROJECTS = TypeAdapter(list[ProjectDTO])
_PROJECT = TypeAdapter(ProjectDetails)
_SERVERS = TypeAdapter(list[ServerDTO])
_USER = TypeAdapter(UserDTO)


def _parse[T](path: str, shape: TypeAdapter[T], payload: object) -> T:
    try:
        return shape.validate_python(payload)
    except ValidationError as error:
        raise APIResponseError(path, error) from error


def _caller(telegram_id: int) -> dict[str, str]:
    """The API scopes reads by the user whose action produced the request."""
    return {"X-Telegram-ID": str(telegram_id)}


class TelegramAPIClient(InternalAPIClient):
    """HTTP client for Telegram bot API usage."""

    def __init__(self) -> None:
        super().__init__(get_settings().api_base_url, timeout=TELEGRAM_TIMEOUT_SECONDS)

    async def get_json(self, path: str, headers: dict | None = None) -> dict | list:
        resp = await self.request("GET", path, headers=headers)
        return resp.json()

    async def post_json(self, path: str, headers: dict | None = None, **kwargs) -> dict:
        resp = await self.request("POST", path, headers=headers, **kwargs)
        return resp.json()

    async def list_projects(self, telegram_id: int) -> list[ProjectDTO]:
        path = "/projects"
        return _parse(path, _PROJECTS, await self.get_json(path, headers=_caller(telegram_id)))

    async def get_project(self, project_id: str, telegram_id: int) -> ProjectDetails:
        path = f"/projects/{project_id}"
        return _parse(path, _PROJECT, await self.get_json(path, headers=_caller(telegram_id)))

    async def list_managed_servers(self, telegram_id: int) -> list[ServerDTO]:
        path = "/servers?is_managed=true"
        return _parse(path, _SERVERS, await self.get_json(path, headers=_caller(telegram_id)))

    async def get_user_by_telegram(self, telegram_id: int) -> UserDTO:
        path = f"users/by-telegram/{telegram_id}"
        return _parse(path, _USER, await self.get_json(path))


api_client = TelegramAPIClient()
