"""Tests for the documented local backend startup configuration."""

from __future__ import annotations

import importlib
from pathlib import Path
import shutil

import pytest

from services.backend.src.core.settings import get_settings


def test_documented_local_environment_constructs_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copying .env.example provides the settings needed at ASGI import time."""
    project_root = Path(__file__).parents[4]
    shutil.copy(project_root / ".env.example", tmp_path / ".env")
    for variable in (
        "APP_NAME",
        "APP_ENV",
        "APP_SECRET_KEY",
        "USERS_GRANT_CAPABILITY",
        "SETTINGS_WRITE_CAPABILITY",
        "JOBS_FIRE_CAPABILITY",
        "DEBUG",
        "ENABLED_MODULES",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_REQUIRE_SSL",
        "SQLALCHEMY_SYNC_DRIVER",
        "SQLALCHEMY_ASYNC_DRIVER",
        "DATABASE_URL",
        "ASYNC_DATABASE_URL",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    settings = get_settings()
    entrypoint = importlib.import_module("services.backend.src.main")
    entrypoint = importlib.reload(entrypoint)

    assert settings.users_grant_capability == "local-grant-capability-not-for-production"
    assert settings.settings_write_capability == "local-settings-capability-not-for-production"
    assert settings.jobs_fire_capability == "local-jobs-capability-not-for-production"
    assert entrypoint.app.title == settings.app_name
    get_settings.cache_clear()
