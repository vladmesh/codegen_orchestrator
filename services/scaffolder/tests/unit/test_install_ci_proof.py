"""CI planner handoff keeps its payload separate from diagnostic output."""

import json
from pathlib import Path
import runpy


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
