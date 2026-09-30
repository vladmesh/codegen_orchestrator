"""The QA catalogue offers a sandbox platform exactly when the QA image installs its tooling.

`shared.contracts.qa_capabilities` cannot see the image builder, so it declares
per platform the package its probes need and whether that package is installed.
This is the one check that keeps that declaration true: a probe route the prompt
offers on an image without its package would fail every run that took it.
"""

from __future__ import annotations

import re

import pytest

from shared.contracts.qa_capabilities import (
    QA_SANDBOX_TOOLING,
    QACapabilityPlatform,
    QACapabilityRoute,
    qa_actions,
)
from shared.contracts.queues.worker import WorkerCapability
from src.image_builder import CAPABILITY_INSTALL_MAP

_QA_SANDBOX = CAPABILITY_INSTALL_MAP[WorkerCapability.QA_SANDBOX.name]


def _installs(package: str) -> bool:
    return any(re.search(rf"\b{re.escape(package)}(==|\s|$)", line) for line in _QA_SANDBOX)


@pytest.mark.parametrize("tooling", QA_SANDBOX_TOOLING, ids=lambda t: t.platform.value)
def test_a_sandbox_platform_is_installed_exactly_when_the_qa_image_installs_its_package(tooling):
    assert tooling.installed is _installs(tooling.package)


def test_telegram_is_telethon_and_web_is_not_installed_yet():
    tooling = {entry.platform: entry for entry in QA_SANDBOX_TOOLING}

    assert tooling[QACapabilityPlatform.TELEGRAM].package == "telethon"
    assert tooling[QACapabilityPlatform.TELEGRAM].installed is True
    assert tooling[QACapabilityPlatform.WEB].installed is False


def test_no_probe_action_is_offered_on_a_platform_the_image_does_not_equip():
    offered = {
        action.platform
        for action in qa_actions()
        if action.route is not QACapabilityRoute.TOOL
        and action.platform in {t.platform for t in QA_SANDBOX_TOOLING}
    }

    assert offered == {t.platform for t in QA_SANDBOX_TOOLING if _installs(t.package)}
