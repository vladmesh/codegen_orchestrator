"""The internal API the situation snapshot reads, served by an ``httpx.MockTransport``.

The snapshot's reader is the production ``ApiSituationReader`` over a real
``InternalAPIClient``; only the transport is a stand-in. So a 404, a body that
is not the DTO and a dropped connection reach the reader exactly as the wire
would deliver them.

Stories and briefs default to what the consumer fixtures already say: a story
is ``gate_stories``'s, and a brief follows ``ordered_stories``'s sets without
counting as one of its audience reads.
"""

from __future__ import annotations

from collections.abc import Callable
import re

import httpx

from shared.clients.internal_api import InternalAPIClient
from tests.unit.factories import make_product_brief

#: A source made to fail, as the reader meets it.
RAISE = "raise"
NOT_FOUND = "404"
MALFORMED = "malformed"

_ROUTES: list[tuple[str, re.Pattern[str]]] = [
    ("deferred", re.compile(r"^/api/stories/owner-notifications/deferred$")),
    ("brief", re.compile(r"^/api/product-briefs/by-story/(?P<id>[^/]+)$")),
    ("story", re.compile(r"^/api/stories/(?P<id>[^/]+)$")),
    ("project_stories", re.compile(r"^/api/stories/$")),
    ("projects", re.compile(r"^/api/projects/$")),
    ("repositories", re.compile(r"^/api/repositories/$")),
    ("applications", re.compile(r"^/api/applications/$")),
]


class SituationApi:
    def __init__(self, stories, ordered) -> None:
        self._stories = stories
        self._ordered = ordered
        #: Brief bodies by story id, over the ``ordered_stories`` default.
        self.briefs: dict[str, dict] = {}
        self.projects: list[dict] = []
        #: Story ids listed per project id; each is read through ``gate_stories``.
        self.project_story_ids: dict[str, list[str]] = {}
        self.repositories: dict[str, list[dict]] = {}
        self.applications: dict[str, list[dict]] = {}
        #: Route name -> RAISE / NOT_FOUND / MALFORMED.
        self.faults: dict[str, str] = {}
        #: Every request, as (route, path, query).
        self.requests: list[tuple[str, str, dict]] = []
        self.client = InternalAPIClient("http://api.test")
        self.client._client = httpx.AsyncClient(
            base_url="http://api.test", transport=httpx.MockTransport(self._handle)
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, dict(request.url.params)
        found = [(route, m) for route, pattern in _ROUTES if (m := pattern.match(path))]
        if not found:
            return httpx.Response(404, json={"detail": f"no route {path}"})
        [(route, match)] = found
        self.requests.append((route, path, query))
        fault = self.faults.get(route)
        if fault == RAISE:
            raise httpx.ConnectError("api unreachable", request=request)
        if fault == NOT_FOUND:
            return httpx.Response(404, json={"detail": "Not Found"})
        if fault == MALFORMED:
            return httpx.Response(200, json={"surprise": True})
        answer: Callable = getattr(self, f"_{route}")
        return await answer(match, query)

    async def _deferred(self, match, query) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def _brief(self, match, query) -> httpx.Response:
        story_id = match["id"]
        if story_id in self.briefs:
            return httpx.Response(200, json=self.briefs[story_id])
        if story_id in self._ordered.unordered:
            return httpx.Response(404, json={"detail": "Not Found"})
        confirmed = {}
        if story_id in self._ordered.unconfirmed:
            confirmed = {"confirmed_at": None, "confirmation_request_id": None}
        brief = make_product_brief(story_id=story_id, **confirmed)
        return httpx.Response(200, json=brief.model_dump(mode="json"))

    async def _story(self, match, query) -> httpx.Response:
        story = await self._stories.get_story(match["id"])
        return httpx.Response(200, json=story.model_dump(mode="json"))

    async def _project_stories(self, match, query) -> httpx.Response:
        ids = self.project_story_ids.get(query["project_id"], [])
        bodies = [(await self._stories.get_story(i)).model_dump(mode="json") for i in ids]
        return httpx.Response(200, json=bodies)

    async def _projects(self, match, query) -> httpx.Response:
        return httpx.Response(200, json=self.projects)

    async def _repositories(self, match, query) -> httpx.Response:
        return httpx.Response(200, json=self.repositories.get(query["project_id"], []))

    async def _applications(self, match, query) -> httpx.Response:
        return httpx.Response(200, json=self.applications.get(query["repo_id"], []))


def project_body(project_id: str, title: str = "Finance bot", owner_id: int = 7) -> dict:
    return {
        "id": project_id,
        "title": title,
        "slug": "finance-bot",
        "status": "active",
        "owner_id": owner_id,
        "created_at": "2026-08-01T10:00:00+00:00",
    }


def repository_body(project_id: str, repo_id: str = "repo-1") -> dict:
    return {
        "id": repo_id,
        "project_id": project_id,
        "name": "finance-bot",
        "git_url": "https://github.com/example/finance-bot.git",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": "2026-08-01T10:00:00+00:00",
    }


def application_body(
    repo_id: str = "repo-1", status: str = "running", last_health_check: str | None = None
) -> dict:
    return {
        "id": 1,
        "repo_id": repo_id,
        "server_handle": "srv-1",
        "service_name": "finance-bot",
        "status": status,
        "last_health_check": last_health_check,
        "created_at": "2026-08-01T10:00:00+00:00",
    }
