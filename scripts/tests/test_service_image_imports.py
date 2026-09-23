"""The image-import guard must cover every Python module Compose starts."""

import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_service_image_imports.py"


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("service_image_imports", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_guard_derives_compose_entrypoint_modules(guard):
    modules = guard.compose_command_modules()

    assert modules["langgraph"] == (
        "src.consumers.architect",
        "src.consumers.deploy",
        "src.consumers.engineering",
        "src.consumers.qa",
    )
    assert modules["scheduler"] == ("src.infrastructure", "src.maintenance", "src.pipeline")
    assert "src.consumers.po" in guard.modules_for("langgraph")


def test_compose_coverage_contract_rejects_an_unimported_module(guard):
    coverage = {
        image.name: guard.modules_for(image.name)
        for image in guard.SERVICE_IMAGES
        if image.name != "scheduler"
    }
    coverage["scheduler"] = ("src.pipeline", "src.infrastructure")

    with pytest.raises(RuntimeError, match="scheduler-maintenance.*src.maintenance"):
        guard.assert_compose_modules_covered(coverage)


def test_every_released_image_is_lock_checked_or_npm_locked(guard):
    """The one image list: each Python image is built here, each frontend runs npm ci."""
    guard.assert_every_listed_image_is_locked()

    listed = {image for image, _dockerfile, _context in guard.listed_service_images()}
    guarded = {image.name for image in guard.SERVICE_IMAGES}
    assert listed - guarded == {"admin-frontend", "user-dashboard"}


def test_a_released_python_image_the_check_does_not_build_fails(guard, monkeypatch):
    kept = tuple(image for image in guard.SERVICE_IMAGES if image.name != "worker-broker")
    monkeypatch.setattr(guard, "SERVICE_IMAGES", kept)

    with pytest.raises(RuntimeError, match="worker-broker"):
        guard.assert_every_listed_image_is_locked()


def test_a_released_image_without_any_lock_fails(guard, monkeypatch):
    listed = [*guard.listed_service_images(), ("unlocked", "services/unlocked/Dockerfile", ".")]
    monkeypatch.setattr(guard, "listed_service_images", lambda: listed)

    with pytest.raises(RuntimeError, match="unlocked .*neither a requirements.lock nor an npm ci"):
        guard.assert_every_listed_image_is_locked()
