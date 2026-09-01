from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from src.routers.product_briefs import require_complete_product_brief_coverage


@pytest.mark.asyncio
async def test_gate_rejects_missing_requirement_even_if_other_criteria_exist() -> None:
    brief = SimpleNamespace(
        id="brief-1",
        confirmed_at=object(),
        content={"must_requirements": [{"id": "must-a"}, {"id": "must-b"}]},
    )
    brief_result = MagicMock()
    brief_result.scalar_one_or_none.return_value = brief
    coverage_result = MagicMock()
    coverage_result.scalars.return_value = iter(["must-a"])
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[brief_result, coverage_result])

    with pytest.raises(HTTPException) as exc:
        await require_complete_product_brief_coverage("story-1", db)

    assert exc.value.status_code == 422
    assert exc.value.detail == {"missing_product_brief_coverage": ["must-b"]}
