"""Regression tests for backend import boundaries and ORM registration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI

from services.backend.src.core.orm import Base


def test_package_imports_do_not_require_application_environment(tmp_path: Path) -> None:
    """Package imports must stay inert until a runtime module is requested explicitly."""

    project_root = Path(__file__).resolve().parents[4]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "APP_NAME",
            "APP_ENV",
            "APP_SECRET_KEY",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_REQUIRE_SSL",
            "DATABASE_URL",
            "ASYNC_DATABASE_URL",
        }
    }
    environment["PYTHONPATH"] = str(project_root)
    script = """
import importlib
import sys

for module in (
    "services.backend.src",
    "services.backend.src.app",
    "services.backend.src.app.api",
    "services.backend.src.app.api.v1",
    "services.backend.src.core",
):
    importlib.import_module(module)

for forbidden in (
    "services.backend.src.main",
    "services.backend.src.app.factory",
    "services.backend.src.app.api.router",
    "services.backend.src.core.settings",
    "services.backend.src.core.db",
):
    assert forbidden not in sys.modules, forbidden
"""

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_model_registry_populates_metadata() -> None:
    """Alembic's explicit registry imports every handwritten model."""

    import services.backend.src.app.models.registry  # noqa: F401

    assert "users" in Base.metadata.tables
    assert "user_channels" in Base.metadata.tables


def test_asgi_entrypoint_builds_the_application() -> None:
    """The explicit factory remains reachable through the ASGI entry point."""

    from services.backend.src.main import app

    assert isinstance(app, FastAPI)
    assert any(getattr(route, "path", None) == "/health" for route in app.routes)


def test_alembic_environment_loads_registered_metadata() -> None:
    """Alembic can load the explicit registry without a running database."""

    project_root = Path(__file__).resolve().parents[4]
    environment = {**os.environ, "DATABASE_URL": "sqlite://"}
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "services/backend/migrations/alembic.ini",
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CREATE TABLE users" in result.stdout
    assert "CREATE TABLE user_channels" in result.stdout
