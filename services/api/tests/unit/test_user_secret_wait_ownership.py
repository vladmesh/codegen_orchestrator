"""Only `park-waiting-user-secret` puts a Story in `waiting_user_secret`.

The secret wait is entered together with the owed ask on the deploy Run, in one
transaction. A second way in — the retired single-hop `wait-user-secret`, a
patched status, a composite chain or a state-wait ending that lands there —
would start a wait nobody owes the owner an ask for. These tests pin the one
entry: the old route is gone, a status write is refused, and no other API code
path lands the status. The composite's own behaviour against Postgres is in
`tests/service/test_lifecycle_wait_actions.py`.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.owner_notification import OWNER_NOTIFICATION_KEY
from shared.contracts.dto.state_wait import TERMINAL_STATUS_BY_ENDING
from shared.contracts.dto.story import StoryStatus
from src.database import get_async_session
from src.main import app
from src.routers._story_actions import COMPOSITE_CHAINS

API_SRC = Path(__file__).resolve().parents[2] / "src"
STORY_ID = "story-abc"
RUN_ID = "deploy-secret-1"
ASK = {"run_id": RUN_ID, "text": "Ask the user for STRIPE_KEY.", "actor": "supervisor"}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _story(status: str) -> MagicMock:
    story = MagicMock()
    story.id = STORY_ID
    story.project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    story.status = status
    story.waiting_on = "none"
    return story


def _deploy_run() -> MagicMock:
    run = MagicMock()
    run.id = RUN_ID
    run.type = "deploy"
    run.story_id = STORY_ID
    run.run_metadata = {}
    run.created_at = datetime.now(UTC)
    return run


def _session(story: MagicMock, run: MagicMock) -> AsyncMock:
    """The locked Story through ``execute``, then the locked Run through ``scalar``."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=story)
    session.execute = AsyncMock(return_value=result)
    session.scalar = AsyncMock(return_value=run)
    session.commit = AsyncMock()

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    return session


async def _post(path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        return await client.post(path, **kwargs)


@pytest.mark.asyncio
async def test_a_deploying_story_enters_the_wait_with_its_ask():
    story, run = _story("deploying"), _deploy_run()
    session = _session(story, run)

    resp = await _post(f"/api/stories/{STORY_ID}/park-waiting-user-secret", json=ASK)

    assert resp.status_code == 200, resp.text  # noqa: PLR2004
    assert resp.json()["disposition"] == "waiting"
    assert story.status == "waiting_user_secret"
    assert run.run_metadata[OWNER_NOTIFICATION_KEY]["event"] == "story_waiting_user_secret"
    assert run.run_metadata[OWNER_NOTIFICATION_KEY]["state"] == "owed"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_story_that_is_not_deploying_is_refused_and_owed_nothing():
    """The invalid-transition case the retired single-hop route's test pinned."""
    story, run = _story("testing"), _deploy_run()
    session = _session(story, run)

    resp = await _post(f"/api/stories/{STORY_ID}/park-waiting-user-secret", json=ASK)

    assert resp.status_code == 422  # noqa: PLR2004
    assert story.status == "testing"
    assert run.run_metadata == {}
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_retired_single_hop_route_is_gone():
    story, run = _story("deploying"), _deploy_run()
    session = _session(story, run)

    resp = await _post(f"/api/stories/{STORY_ID}/wait-user-secret")

    assert resp.status_code in (404, 405)
    assert story.status == "deploying"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_status_write_to_the_wait_is_refused():
    story, run = _story("deploying"), _deploy_run()
    session = _session(story, run)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.patch(
            f"/api/stories/{STORY_ID}", json={"status": "waiting_user_secret"}
        )

    assert resp.status_code == 422  # noqa: PLR2004
    assert story.status == "deploying"
    session.commit.assert_not_awaited()


def _functions_landing_the_wait() -> set[str]:
    """Every API function that passes WAITING_USER_SECRET to a Story status writer."""
    landing: set[str] = set()
    for path in API_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call in ast.walk(function):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in {"_do_transition", "_land_on"}
                ):
                    continue
                if any(
                    isinstance(arg, ast.Attribute) and arg.attr == "WAITING_USER_SECRET"
                    for arg in call.args
                ):
                    landing.add(f"{path.relative_to(API_SRC)}:{function.name}")
    return landing


def test_no_other_api_path_lands_a_story_in_the_wait():
    assert _functions_landing_the_wait() == {"routers/_story_actions.py:park_waiting_user_secret"}
    assert all(StoryStatus.WAITING_USER_SECRET not in chain for chain in COMPOSITE_CHAINS.values())
    assert StoryStatus.WAITING_USER_SECRET not in TERMINAL_STATUS_BY_ENDING.values()
