"""A deploy that names no story still finds the project's confirmed settings.

The lookup takes the confirmed briefs newest first and returns the first one
that carries `initial_settings`; a project with none answers 404, which the
consumer reads as "nothing to seed".
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.product_brief import InitialSetting, MustRequirement, ProductBriefContent
from src.routers import product_briefs
from src.routers.product_briefs import get_project_initial_settings_brief

PROJECT_ID = uuid.UUID("202a5f2b-3e87-4f32-bbde-84d31a004e14")


def _brief(brief_id: str, settings: list[InitialSetting]) -> SimpleNamespace:
    content = ProductBriefContent(
        summary="Finance bot",
        must_requirements=[MustRequirement(id="spend", text="Record a spend")],
        initial_settings=settings,
    )
    return SimpleNamespace(
        id=brief_id,
        project_id=PROJECT_ID,
        story_id=None,
        revision=1,
        title="Finance bot",
        content=content.model_dump(mode="json"),
        confirmed_at=datetime.now(UTC),
        confirmation_request_id="confirm",
        coverage_admitted_at=None,
        planning_attempt_id=None,
        planning_attempt_active=False,
        planning_attempt_heartbeat_at=None,
    )


def _session(briefs):
    result = MagicMock()
    result.scalars.return_value.all.return_value = briefs
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


async def _lookup(briefs):
    with patch.object(product_briefs, "_authorize", new=AsyncMock()) as authorize:
        read = await get_project_initial_settings_brief(
            PROJECT_ID, x_telegram_id=None, db=_session(briefs), internal=True, credentials=None
        )
    authorize.assert_awaited_once()
    return read


@pytest.mark.asyncio
async def test_the_newest_confirmed_brief_with_settings_is_chosen():
    read = await _lookup(
        [
            _brief("brief-newest-no-settings", []),
            _brief(
                "brief-58d709d8f41b921b301d2b09",
                [
                    InitialSetting(key="finance.currency", value="USD"),
                    InitialSetting(key="interface.language", value="ru"),
                ],
            ),
            _brief("brief-older", [InitialSetting(key="finance.currency", value="EUR")]),
        ]
    )

    assert read.id == "brief-58d709d8f41b921b301d2b09"
    assert [s.key for s in read.content.initial_settings] == [
        "finance.currency",
        "interface.language",
    ]


@pytest.mark.asyncio
async def test_a_project_without_confirmed_settings_answers_not_found():
    with pytest.raises(HTTPException) as refused:
        await _lookup([_brief("brief-no-settings", [])])

    assert refused.value.status_code == 404
