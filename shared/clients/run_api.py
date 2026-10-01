"""Shared typed client seam for the Run API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from shared.contracts.dto.run import RunDTO, RunUpdate


def _run_update_payload(update: RunUpdate | Mapping[str, Any]) -> dict[str, Any]:
    """Validate a partial Run update before it crosses the service boundary."""
    typed = update if isinstance(update, RunUpdate) else RunUpdate.model_validate(dict(update))
    return typed.model_dump(mode="json", exclude_unset=True)


class RunAPIClientMixin:
    """Run reads and writes shared by internal service clients.

    Concrete clients provide request through InternalAPIClient. Keeping the
    typed PATCH seam here prevents each service from reconstructing the Run
    contract with ad-hoc dictionaries.
    """

    async def get_run(self, run_id: str) -> RunDTO:
        response = await self.request("GET", f"runs/{run_id}")  # type: ignore[attr-defined]
        return RunDTO.model_validate(response.json())

    async def update_run(self, run_id: str, update: RunUpdate | Mapping[str, Any]) -> None:
        await self.request(  # type: ignore[attr-defined]
            "PATCH",
            f"runs/{run_id}",
            json=_run_update_payload(update),
        )

    async def record_run_outcome_unless_settled(
        self, run_id: str, update: RunUpdate | Mapping[str, Any]
    ) -> bool:
        """Write a terminal outcome unless a competing settlement landed first."""
        try:
            await self.request(  # type: ignore[attr-defined]
                "PATCH",
                f"runs/{run_id}",
                json=_run_update_payload(update),
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != httpx.codes.CONFLICT:
                raise
            return False
        return True
