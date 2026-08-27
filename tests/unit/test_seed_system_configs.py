"""Behaviour tests for the system-config seeder against a fake config API.

The fake mirrors the real router: GET returns 404 for an unknown key, POST is an
upsert that answers 201 either way.
"""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml

from scripts import seed_system_configs as seeder

API_BASE_URL = "http://api:8000"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Captured before patching: the seeder shares the httpx module object with us,
# so the factories below would otherwise call themselves.
_REAL_CLIENT = httpx.Client


@pytest.fixture
def db() -> dict[str, dict]:
    return {}


@pytest.fixture
def fake_api(db):
    """Patch httpx.Client inside the seeder to talk to an in-memory config API."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/api/system-configs/")
        if request.method == "GET":
            if key not in db:
                return httpx.Response(404, json={"detail": f"'{key}' not found"})
            return httpx.Response(200, json=db[key])
        if request.method == "POST":
            payload = json.loads(request.content.decode())
            db[payload["key"]] = payload
            return httpx.Response(201, json=payload)
        raise AssertionError(f"unexpected {request.method} {request.url}")

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        return _REAL_CLIENT(transport=transport, **kwargs)

    with patch.object(seeder.httpx, "Client", client_factory):
        yield


def _write_configs(tmp_path: Path, configs: list[dict]) -> Path:
    path = tmp_path / "system_configs.yaml"
    path.write_text(yaml.safe_dump(configs), encoding="utf-8")
    return path


def _config(key: str, value) -> dict:
    return {"key": key, "value": value, "category": "scheduler", "description": key}


def test_file_value_overwrites_a_diverged_db_value(tmp_path, db, fake_api, capsys):
    db["scheduler.service_template_ref"] = _config("scheduler.service_template_ref", "0.3.4")
    path = _write_configs(tmp_path, [_config("scheduler.service_template_ref", "0.3.6")])

    assert seeder.seed_system_configs(API_BASE_URL, path) is True

    assert db["scheduler.service_template_ref"]["value"] == "0.3.6"
    out = capsys.readouterr().out
    assert "scheduler.service_template_ref" in out
    assert "0.3.4" in out and "0.3.6" in out


def test_missing_key_is_created(tmp_path, db, fake_api):
    path = _write_configs(tmp_path, [_config("scheduler.dispatch_interval_seconds", 30)])

    assert seeder.seed_system_configs(API_BASE_URL, path) is True
    assert db["scheduler.dispatch_interval_seconds"]["value"] == 30


def test_key_absent_from_the_file_is_left_alone(tmp_path, db, fake_api):
    db["ops.manual_only"] = _config("ops.manual_only", "hand-tuned")
    path = _write_configs(tmp_path, [_config("scheduler.dispatch_interval_seconds", 30)])

    assert seeder.seed_system_configs(API_BASE_URL, path) is True
    assert db["ops.manual_only"]["value"] == "hand-tuned"


def test_matching_value_is_not_rewritten(tmp_path, db, fake_api, capsys):
    db["scheduler.dispatch_interval_seconds"] = _config("scheduler.dispatch_interval_seconds", 30)
    path = _write_configs(tmp_path, [_config("scheduler.dispatch_interval_seconds", 30)])

    assert seeder.seed_system_configs(API_BASE_URL, path) is True
    # No POST was made: the record is still the one the fake API started with.
    assert "updated_by" not in db["scheduler.dispatch_interval_seconds"]
    assert "1 already in sync" in capsys.readouterr().out


def test_write_failure_is_reported(tmp_path, db, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    path = _write_configs(tmp_path, [_config("scheduler.dispatch_interval_seconds", 30)])

    with patch.object(
        seeder.httpx, "Client", lambda *a, **kw: _REAL_CLIENT(transport=transport, **kw)
    ):
        assert seeder.seed_system_configs(API_BASE_URL, path) is False

    assert "Failed to write" in capsys.readouterr().out


def test_api_service_test_overlay_explicitly_overrides_every_work_admission_key():
    """The shared service-test database must not exhaust production ceilings."""
    configs = seeder.load_configs(REPO_ROOT / "scripts/system_configs.service_test.yaml")
    values = {config["key"]: config["value"] for config in configs}

    assert values == {
        "work_admission.max_projects_per_user": 10000,
        "work_admission.max_concurrent_paid_runs": 10000,
        "work_admission.emergency_stop": False,
        "work_admission.engineering_executor_override": "none",
        "work_admission.qa_executor_override": "none",
    }


def test_paid_work_controls_seed_through_the_initialize_only_typed_command(
    tmp_path, db, monkeypatch
):
    """A blank database must not need generic CRUD for protected controls."""
    configs = [
        _config("work_admission.emergency_stop", False),
        _config("work_admission.max_concurrent_paid_runs", 7),
        _config("work_admission.engineering_executor_override", "claude"),
        _config("work_admission.qa_executor_override", "codex"),
    ]
    path = _write_configs(tmp_path, configs)
    typed_requests: list[dict] = []
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/work-admission/controls/initialize":
            assert request.method == "POST"
            typed_requests.append(json.loads(request.content.decode()))
            return httpx.Response(200, json=typed_requests[-1])
        raise AssertionError(
            f"protected controls used generic CRUD: {request.method} {request.url}"
        )

    transport = httpx.MockTransport(handler)
    with patch.object(
        seeder.httpx, "Client", lambda *a, **kw: _REAL_CLIENT(transport=transport, **kw)
    ):
        assert seeder.seed_system_configs(API_BASE_URL, path) is True

    assert typed_requests == [
        {
            "emergency_stop": False,
            "max_concurrent_paid_runs": 7,
            "engineering_executor_override": "claude",
            "qa_executor_override": "codex",
        }
    ]


def test_paid_work_seed_contract_has_no_operator_mutation_path():
    """Deploy seeding may initialize absent controls but never replace live policy."""
    source = (REPO_ROOT / "scripts/seed_system_configs.py").read_text(encoding="utf-8")

    assert '"POST", "work-admission/controls/initialize"' in source
    assert '"PUT", "work-admission/controls", json=paid_work_controls' not in source
    assert '"system-configs/", json=payload' in source


def test_api_service_test_overlay_initializes_before_production_defaults():
    """The service-test startup path must not mutate protected controls after seeding."""
    source = (REPO_ROOT / "services/api/entrypoint.sh").read_text(encoding="utf-8")

    assert source.index('"$SYSTEM_CONFIGS_TEST_OVERLAY"') < source.index(
        "/app/scripts/system_configs.yaml"
    )
    assert "--skip-key work_admission.max_projects_per_user" in source
