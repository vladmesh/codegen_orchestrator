"""Atomic project config key updates keep unrelated concurrent state."""

import uuid

import pytest

from shared.contracts.dto.project import ProjectConfigPatch
from src.routers.projects import lifecycle

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Project:
    def __init__(self) -> None:
        self.id = PROJECT_ID
        self.config = {
            "agent_type": "codex",
            "owner_key": "fresh-value",
            "scaffold_error": "old error",
            "secrets": {"ciphertext": "opaque"},
        }


class _Session:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row) -> None:
        return None


@pytest.mark.asyncio
async def test_config_patch_preserves_unowned_keys(monkeypatch):
    project = _Project()
    session = _Session()

    async def locked(db, project_id):
        assert db is session
        assert project_id == PROJECT_ID
        return project

    async def access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(lifecycle, "load_locked_project", locked)
    monkeypatch.setattr(lifecycle, "check_project_access", access)

    result = await lifecycle.patch_project_config(
        PROJECT_ID,
        ProjectConfigPatch(values={"tree": ".\n-- src"}, remove=["scaffold_error"]),
        x_telegram_id=None,
        db=session,
        _is_internal=True,
        credentials=None,
    )

    assert session.committed
    assert result.config == {
        "agent_type": "codex",
        "owner_key": "fresh-value",
        "secrets": {"ciphertext": "opaque"},
        "tree": ".\n-- src",
    }


def test_config_patch_rejects_set_and_remove_of_same_key():
    with pytest.raises(ValueError):
        ProjectConfigPatch(values={"tree": "new"}, remove=["tree"])
