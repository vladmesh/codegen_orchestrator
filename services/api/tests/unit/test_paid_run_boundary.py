"""The generic Run endpoint must not bypass paid-work admission."""

from pathlib import Path


def test_generic_run_endpoint_rejects_paid_run_types() -> None:
    source = Path("services/api/src/routers/runs.py").read_text()

    assert "run.type in (RunType.ENGINEERING.value, RunType.QA.value)" in source
    assert '"Paid coding-agent runs must use the paid-run start command"' in source
