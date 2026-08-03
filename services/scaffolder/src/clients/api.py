"""Thin HTTP client for scaffolder API calls."""

from __future__ import annotations

import structlog

from shared.clients.internal_api import InternalAPIClient
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.story import StoryDTO
from src.config import get_settings

logger = structlog.get_logger(__name__)

_client: ScaffolderAPIClient | None = None


def get_api_client() -> ScaffolderAPIClient:
    global _client
    if _client is None:
        _client = ScaffolderAPIClient()
    return _client


class ScaffolderAPIClient(InternalAPIClient):
    """HTTP client for project/repository updates."""

    def __init__(self) -> None:
        super().__init__(get_settings().api_base_url)

    async def get_project(self, project_id: str) -> ProjectDTO:
        resp = await self.request("GET", f"projects/{project_id}")
        return ProjectDTO.model_validate(resp.json())

    async def get_repository(self, repo_id: str) -> dict:
        resp = await self.request("GET", f"repositories/{repo_id}")
        return resp.json()

    async def update_project_status(self, project_id: str, status: str) -> None:
        await self.request(
            "PATCH",
            f"projects/{project_id}",
            json={"status": status},
        )
        logger.info("project_status_updated", project_id=project_id, status=status)

    async def update_repository(self, repo_id: str, **fields) -> None:
        await self.request("PATCH", f"repositories/{repo_id}", json=fields)
        logger.info("repository_updated", repo_id=repo_id, fields=list(fields.keys()))

    async def update_project_config(self, project_id: str, config: dict) -> None:
        await self.request(
            "PATCH",
            f"projects/{project_id}",
            json={"config": config},
        )
        logger.info("project_config_updated", project_id=project_id)

    async def get_stories_by_project(self, project_id: str) -> list[StoryDTO]:
        resp = await self.request("GET", f"stories/?project_id={project_id}")
        return [StoryDTO.model_validate(s) for s in resp.json()]

    async def fail_story(self, story_id: str) -> None:
        await self.request(
            "POST",
            f"stories/{story_id}/fail",
            json={"actor": "scaffolder"},
        )
        logger.info("story_failed", story_id=story_id)
