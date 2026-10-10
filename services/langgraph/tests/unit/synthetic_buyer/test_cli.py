"""The operator's offline commands, and the live ones' refusals before anything connects."""

from __future__ import annotations

import json

import pytest

from src.synthetic_buyer.__main__ import EXIT_CONFIG, EXIT_REFUSED, main
from src.synthetic_buyer.evidence import EvidenceStore, Redaction, new_record
from tests.unit.synthetic_buyer.fakes import ENVIRON, REVISION, config_data


def _config(tmp_path, **overrides) -> str:
    path = tmp_path / "buyer.json"
    path.write_text(json.dumps(config_data(evidence_dir=str(tmp_path / "evidence"), **overrides)))
    return str(path)


def test_check_reports_handles_present_and_writes_nothing(tmp_path):
    assert main(["check", "--config", _config(tmp_path)], ENVIRON) == 0
    assert not (tmp_path / "evidence").exists()


def test_check_fails_while_a_handle_is_missing(tmp_path):
    environ = {**ENVIRON, "TELETHON_SESSION": ""}

    assert main(["check", "--config", _config(tmp_path)], environ) == 1


def test_an_invalid_config_is_a_config_refusal(tmp_path):
    path = tmp_path / "buyer.json"
    path.write_text(json.dumps({"schema_version": 1}))

    assert main(["check", "--config", str(path)], ENVIRON) == EXIT_CONFIG


def test_run_refuses_an_operation_whose_evidence_exists(tmp_path):
    config = _config(tmp_path)
    store = EvidenceStore(tmp_path / "evidence" / "s1487-buyer-001", Redaction())
    store.record = new_record(
        operation_id="s1487-buyer-001", revision=REVISION, handles={}, now="x"
    )
    store.save()

    code = main(["run", "--config", config, "--orchestrator-revision", REVISION], ENVIRON)

    assert code == EXIT_REFUSED


@pytest.mark.parametrize("command", ["resume", "cleanup"])
def test_resume_and_cleanup_need_retained_evidence(tmp_path, command):
    code = main(
        [command, "--config", _config(tmp_path), "--orchestrator-revision", REVISION], ENVIRON
    )

    assert code == EXIT_REFUSED
    assert not (tmp_path / "evidence").exists()


def test_a_live_command_with_a_missing_handle_writes_no_evidence(tmp_path):
    environ = {**ENVIRON, "PLATFORM_AUTH_ADMIN_TOKEN": ""}

    code = main(
        ["run", "--config", _config(tmp_path), "--orchestrator-revision", REVISION], environ
    )

    assert code == EXIT_CONFIG
    assert not (tmp_path / "evidence").exists()


def test_a_live_command_names_the_exact_released_revision(tmp_path):
    with pytest.raises(SystemExit):
        main(["run", "--config", _config(tmp_path), "--orchestrator-revision", "main"], ENVIRON)


def test_inspect_reads_retained_evidence_offline(tmp_path):
    config = _config(tmp_path)
    assert main(["inspect", "--config", config], ENVIRON) == EXIT_REFUSED
    store = EvidenceStore(tmp_path / "evidence" / "s1487-buyer-001", Redaction())
    store.record = new_record(
        operation_id="s1487-buyer-001", revision=REVISION, handles={}, now="x"
    )
    store.save()

    assert main(["inspect", "--config", config], ENVIRON) == 0
