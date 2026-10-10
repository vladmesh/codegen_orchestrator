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


def _retained(tmp_path, **record) -> EvidenceStore:
    store = EvidenceStore(tmp_path / "evidence" / "s1487-buyer-001", Redaction())
    store.record = (
        new_record(operation_id="s1487-buyer-001", revision=REVISION, handles={}, now="x") | record
    )
    store.save()
    return store


@pytest.mark.parametrize("command", ["check", "inspect"])
def test_offline_commands_touch_no_port_even_with_a_pending_send(tmp_path, monkeypatch, command):
    """`check` and `inspect` read files only: no Telegram, API or identity lease is built."""
    from src.synthetic_buyer import __main__ as cli

    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline command built a live port")

    monkeypatch.setattr(cli, "live_buyer", refuse)
    monkeypatch.setattr(cli, "identity_lease", refuse)
    config = _config(tmp_path)
    store = _retained(
        tmp_path,
        pending={"effect": "send", "dialog": "codegen", "kind": "persona", "after": 1},
    )
    before = store.path.read_text()

    assert main([command, "--config", config], ENVIRON) == 0
    assert store.path.read_text() == before


@pytest.mark.parametrize("command", ["check", "inspect", "resume", "cleanup"])
def test_evidence_of_another_schema_is_refused_and_left_as_it_is(tmp_path, command):
    config = _config(tmp_path)
    store = _retained(tmp_path, schema_version=1)
    before = store.path.read_text()
    revision = [] if command in {"check", "inspect"} else ["--orchestrator-revision", REVISION]

    code = main([command, "--config", config, *revision], ENVIRON)

    assert code == (1 if command == "check" else EXIT_REFUSED)
    assert store.path.read_text() == before


def test_the_identity_command_releases_only_the_exact_held_token(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from fakeredis.aioredis import FakeRedis

    from src.consumers._qa_telegram_lease import Holder, HolderKind, TelegramIdentityLease
    from src.synthetic_buyer import __main__ as cli

    redis = FakeRedis()

    @asynccontextmanager
    async def lease(config, environ, redaction, clock):
        yield TelegramIdentityLease(redis, config.buyer.telegram_id)

    monkeypatch.setattr(cli, "identity_lease", lease)
    config = _config(tmp_path)

    async def orphan() -> str:
        held = TelegramIdentityLease(redis, 8202532144)
        return await held._acquire(  # noqa: SLF001 - a process killed while holding
            Holder(HolderKind.NATIVE_QA, "qa-run-9", "exploratory"), 0, 0
        )

    import asyncio

    token = asyncio.run(orphan())

    assert main(["identity", "--config", config], ENVIRON) == 0
    assert main(["identity", "--config", config, "--release", "wrong"], ENVIRON) == EXIT_REFUSED
    assert main(["identity", "--config", config, "--release", token], ENVIRON) == 0
    assert main(["identity", "--config", config, "--release", token], ENVIRON) == EXIT_REFUSED
