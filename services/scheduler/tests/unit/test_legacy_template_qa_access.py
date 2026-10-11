"""A legacy service-template bot is QA'd through its env allowlist, not a kit grant."""

from types import SimpleNamespace
import uuid

import structlog

from src.tasks.supervisor.handoff import _temporary_access_is_needed


def _project(config: dict) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), config=config)


def test_legacy_template_bot_needs_no_temporary_access():
    project = _project(
        {
            "modules": ["backend", "tg_bot"],
            "service_template": {"source": "gh:vladmesh/service-template", "commit": "0.3.6"},
        }
    )

    assert _temporary_access_is_needed(project, None, structlog.get_logger()) is False


def test_kit_bot_still_needs_temporary_access():
    project = _project(
        {
            "modules": ["backend", "tg_bot"],
            "service_template": {"source": "gh:vladmesh/codegen-product-kit", "commit": "2.5.0"},
        }
    )

    assert _temporary_access_is_needed(project, None, structlog.get_logger()) is True
