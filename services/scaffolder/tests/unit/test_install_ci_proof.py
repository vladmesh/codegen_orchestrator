"""CI planner handoff keeps its payload separate from diagnostic output."""

import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from scripts.template_pin import TEMPLATE_PIN


def test_planner_payload_is_not_decoded_from_logged_stdout(monkeypatch):
    script = (
        Path(__file__).resolve().parents[4]
        / "tests/integration/template/mechanical_install_smoke.py"
    )
    namespace = runpy.run_path(str(script))
    expected = {"package": {"name": "reminders"}, "libraries": [{"name": "textparse"}]}

    def logged_planner(argv, cwd, env):
        if len(argv) == 4:
            Path(argv[3]).write_text(json.dumps(expected))
        return "2026-10-05 [info] catalog_loaded\n"

    monkeypatch.setitem(namespace["select_payload"].__globals__, "run", logged_planner)
    assert namespace["select_payload"]() == expected


@pytest.mark.subprocess
def test_ci_notes_customization_runs_registered_save_and_list(tmp_path):
    root = Path(__file__).resolve().parents[4]
    script = root / "tests/integration/template/mechanical_install_smoke.py"
    product = tmp_path / "product"
    shutil.copytree(TEMPLATE_PIN.fixture_path(root), product)
    namespace = runpy.run_path(str(script))
    namespace["customize_notes"](product)
    # Fresh interpreter avoids the orchestrator's shared package. Broker I/O is
    # irrelevant to notes commands; the released CI product uses its real library.
    code = """
import sys
sys.path[:0] = ['.', 'shared']
import asyncio, runpy, pytest
from types import ModuleType
from unittest.mock import AsyncMock
events = ModuleType('shared.generated.events')
events.get_broker = AsyncMock()
events.publish_command_received = AsyncMock()
sys.modules['shared.generated.events'] = events
test = runpy.run_path('services/tg_bot/tests/unit/test_retained_notes.py')
with pytest.MonkeyPatch.context() as monkeypatch:
    asyncio.run(test['test_registered_notes_save_and_list'](monkeypatch))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=product,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
