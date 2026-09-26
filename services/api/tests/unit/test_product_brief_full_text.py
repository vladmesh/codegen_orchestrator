"""The admin API reads a brief's full form: every section, one item each.

The same pure renderer the PO's `show_full_brief` uses, so the operator reads
exactly the text a user who asked for the whole brief is sent — as a list here,
joined by `MESSAGE_BREAK` there.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.product_brief import ProductBriefContent
from shared.product_brief_text import render_full_brief_sections
from src.routers import product_briefs
from src.routers.product_briefs import get_product_brief_full_text

PROJECT_ID = uuid.UUID("202a5f2b-3e87-4f32-bbde-84d31a004e14")

CONTENT = {
    "summary": "Бот учёта расходов & доходов",
    "language": "ru",
    "must_requirements": [
        {"id": "spend", "text": "Записывает расход <b>", "user_wording": "считай траты"}
    ],
    "usage_examples": [
        {"requirement_id": "spend", "user_sends": "кофе 250", "product_answers": "Записал"}
    ],
    "limitations": ["Только текстом"],
    "initial_settings": [],
}


def _row(content: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="brief-1",
        project_id=PROJECT_ID,
        story_id=None,
        revision=3,
        title="Финансы",
        content=content,
        confirmed_at=datetime.now(UTC),
        confirmation_request_id="confirm",
        coverage_admitted_at=None,
        planning_attempt_id=None,
        planning_attempt_active=False,
        planning_attempt_heartbeat_at=None,
    )


def _session(row):
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


async def _full(row):
    with patch.object(product_briefs, "_authorize", new=AsyncMock()) as authorize:
        read = await get_product_brief_full_text(
            "brief-1", x_telegram_id=None, db=_session(row), internal=True, credentials=None
        )
    authorize.assert_awaited_once()
    return read


@pytest.mark.asyncio
async def test_the_full_form_is_returned_as_sections_in_reading_order():
    read = await _full(_row(CONTENT))

    assert read.brief_id == "brief-1"
    assert read.revision == 3
    assert read.language == "ru"
    assert read.sections == render_full_brief_sections(
        "Финансы", ProductBriefContent.model_validate(CONTENT)
    )
    assert [section.split("\n", maxsplit=1)[0] for section in read.sections] == [
        "<b>Финансы</b>",
        "<b>Что вы получите</b>",
        "<b>Как вы будете пользоваться</b>",
        "<b>Ограничения</b>",
    ]
    assert "&amp;" in read.sections[0]
    assert "Записывает расход &lt;b&gt;" in read.sections[1]


@pytest.mark.asyncio
async def test_a_brief_stored_over_todays_caps_still_reads_in_full():
    """Only new proposals are capped; what was stored before stays readable."""
    oversized = {**CONTENT, "summary": "s" * 5000}

    read = await _full(_row(oversized))

    assert "s" * 5000 in read.sections[0]


@pytest.mark.asyncio
async def test_an_unknown_brief_is_not_found():
    with pytest.raises(HTTPException) as refused:
        await _full(None)

    assert refused.value.status_code == 404
