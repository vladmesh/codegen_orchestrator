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


def test_every_image_is_one_target_of_one_bake_loaded_under_its_tag(guard):
    """One bake builds them all in parallel; each lands in the daemon the checks run on."""
    definition = guard.bake_definition(guard.SERVICE_IMAGES, None)

    assert definition["group"]["default"]["targets"] == [
        image.name for image in guard.SERVICE_IMAGES
    ]
    for image in guard.SERVICE_IMAGES:
        target = definition["target"][image.name]
        assert target["dockerfile"] == image.dockerfile
        assert target["context"] == str(guard.ROOT)
        assert target["tags"] == [image.tag]
        assert target["output"] == ["type=docker"]
        assert "cache-from" not in target


def test_with_the_gha_cache_each_target_uses_the_scope_of_its_dockerfile(guard):
    """The scope the test jobs build the same Dockerfile through, not one per job."""
    definition = guard.bake_definition(guard.SERVICE_IMAGES, "gha")

    api = definition["target"]["api"]
    assert api["cache-from"] == ["type=gha,scope=buildx-services_api_Dockerfile"]
    assert api["cache-to"] == [
        "type=gha,scope=buildx-services_api_Dockerfile,mode=max,ignore-error=true"
    ]
    scopes = {target["cache-from"][0] for target in definition["target"].values()}
    assert len(scopes) == len(guard.SERVICE_IMAGES)


def test_every_built_image_is_still_lock_checked(guard, monkeypatch):
    """The parallel checks still run the lock comparison on every image the bake built."""
    built, probed = [], []
    monkeypatch.setattr(guard, "build_images", lambda images, cache: built.extend(images))
    monkeypatch.setattr(guard, "run", lambda command: None)

    real_capture = guard.capture

    def capture(command):
        if command[0] != "docker":
            return real_capture(command)
        probed.append(command[command.index("python") + 1])
        return "{}"

    monkeypatch.setattr(guard, "capture", capture)
    checked = []
    monkeypatch.setattr(
        guard.service_image_locks,
        "check_image",
        lambda name, lock, pyproject, probe: checked.append(name) or [],
    )

    guard.main(["--layer-cache", "gha"])

    assert built == list(guard.SERVICE_IMAGES)
    assert sorted(checked) == sorted(image.name for image in guard.SERVICE_IMAGES)
    assert sorted(probed) == sorted(image.tag for image in guard.SERVICE_IMAGES)


def test_a_drifted_image_still_fails_the_check(guard, monkeypatch):
    monkeypatch.setattr(guard, "build_images", lambda images, cache: None)
    monkeypatch.setattr(guard, "run", lambda command: None)
    real_capture = guard.capture
    monkeypatch.setattr(
        guard, "capture", lambda command: "{}" if command[0] == "docker" else real_capture(command)
    )
    monkeypatch.setattr(
        guard.service_image_locks,
        "check_image",
        lambda name, lock, pyproject, probe: [f"{name}: drift"] if name == "scheduler" else [],
    )

    with pytest.raises(SystemExit) as exit_info:
        guard.main([])

    assert exit_info.value.code == 1
