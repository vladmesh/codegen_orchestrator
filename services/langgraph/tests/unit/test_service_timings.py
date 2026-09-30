"""The native service proof reports every pytest phase, including fixture work."""

from pathlib import Path

import yaml


def test_service_runner_reports_all_phase_durations():
    root = Path(__file__).resolve().parents[4]
    compose = yaml.safe_load((root / "tests/compose/service/langgraph.yml").read_text())
    command = compose["services"]["langgraph-test-runner"]["command"]
    assert "--durations=0" in command
    assert "--durations-min=0" in command
    assert "-ra" in command
