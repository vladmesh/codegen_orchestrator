"""Keep the five production admin surfaces aligned with authoritative API shapes."""

from pathlib import Path
import re

import pytest

from shared.contracts.dto.executor_diagnostics import (
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.dto.work_admission import PaidWorkControlsCommand
from src.schemas.project import ProjectRead
from src.schemas.task import TaskRead
from src.schemas.user import UserRead

FRONTEND_TYPES = Path(__file__).resolve().parents[4] / "services/admin-frontend/src/types/api.ts"


def _interface_fields(source: str, name: str) -> set[str]:
    match = re.search(rf"export interface {name} \{{(?P<body>.*?)\n\}}", source, re.DOTALL)
    assert match, f"{name} is missing from the admin contract"
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??:", match.group("body"), re.MULTILINE))


def _assert_exact_fields(source: str, name: str, expected: set[str]) -> None:
    actual = _interface_fields(source, name)
    assert actual == expected, f"{name} drifted: expected {expected}, got {actual}"


def _assert_no_obsolete_queue_map(source: str) -> None:
    assert "export interface QueueHealth" not in source


def test_admin_frontend_contract_gate_matches_current_response_and_request_shapes():
    source = FRONTEND_TYPES.read_text()

    _assert_exact_fields(source, "User", set(UserRead.model_fields))
    _assert_exact_fields(source, "Project", set(ProjectRead.model_fields))
    _assert_exact_fields(source, "Task", set(TaskRead.model_fields))
    _assert_exact_fields(source, "PaidWorkControls", set(PaidWorkControlsCommand.model_fields))
    _assert_exact_fields(source, "ExecutorDiagnostic", set(ExecutorDiagnostic.model_fields))
    _assert_exact_fields(
        source, "ExecutorDiagnosticSnapshot", set(ExecutorDiagnosticSnapshot.model_fields)
    )
    assert "'waiting_resources'" in source
    _assert_no_obsolete_queue_map(source)
    assert "name: string" not in re.search(
        r"export interface Project \{.*?\n\}", source, re.DOTALL
    ).group(0)


def test_contract_gate_demonstrably_rejects_the_obsolete_admin_shapes():
    with pytest.raises(AssertionError):
        _assert_exact_fields(
            "export interface Project {\n  name: string\n}", "Project", {"id", "title"}
        )
    with pytest.raises(AssertionError):
        _assert_no_obsolete_queue_map("export interface QueueHealth {}")
    with pytest.raises(AssertionError):
        _assert_exact_fields(
            "export interface PaidWorkControls {\n  emergency_stop: boolean\n}",
            "PaidWorkControls",
            set(PaidWorkControlsCommand.model_fields),
        )
