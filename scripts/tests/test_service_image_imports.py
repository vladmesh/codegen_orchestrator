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
