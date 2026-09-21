"""Boundary guards for ledger-owned engineering accounting."""

from pathlib import Path
import re

from shared.models import Run
from src.schemas.run import RunRead, RunUpdate

ROOT = Path(__file__).resolve().parents[4]
LEGACY_RUN_ACCOUNTING_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}
)


def test_run_contract_and_model_do_not_own_engineering_accounting() -> None:
    assert LEGACY_RUN_ACCOUNTING_FIELDS.isdisjoint(RunRead.model_fields)
    assert LEGACY_RUN_ACCOUNTING_FIELDS.isdisjoint(RunUpdate.model_fields)
    assert LEGACY_RUN_ACCOUNTING_FIELDS.isdisjoint(Run.__table__.columns.keys())


def test_run_consumers_do_not_restore_legacy_accounting_surface() -> None:
    router = (ROOT / "services/api/src/routers/runs.py").read_text()
    assert "_attach_ledger_compatibility" not in router
    assert "_ledger_total_tokens" not in router
    assert "_ledger_cost_usd" not in router

    frontend = (ROOT / "services/admin-frontend/src/types/api.ts").read_text()
    match = re.search(r"export interface Run \{(?P<body>.*?)\n\}", frontend, re.DOTALL)
    assert match is not None
    run_interface = match.group("body")
    for field in LEGACY_RUN_ACCOUNTING_FIELDS:
        assert re.search(rf"^  {field}:", run_interface, re.MULTILINE) is None

    dashboard = (ROOT / "infra/grafana/dashboards/run-operations.json").read_text()
    assert "engineering_attempt_ledger" in dashboard
    assert "r.total_tokens" not in dashboard
    assert "r.cost_usd" not in dashboard
