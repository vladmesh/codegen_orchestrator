from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess

import pytest

from scripts.stand_lifecycle import (
    BitLaunchLifecycle,
    LifecycleRefusal,
    Machine,
    RunTagDestructionPolicy,
    cents_to_usd,
    select_expired_run_machines,
)


def _user(**overrides):
    return {
        "balance": 19532,
        "used": 0,
        "limit": 5,
        "emailConfirmed": True,
        "costPerHr": 42,
        **overrides,
    }


def _keys(*, present=True):
    return {"keys": [{"id": "key-1", "name": "stands_ed25519"}] if present else []}


def test_balance_cents_are_rendered_without_float_drift():
    assert cents_to_usd(19532) == "19.53"


@pytest.mark.parametrize(
    ("user", "keys", "reason"),
    [
        (_user(balance=199), _keys(), "insufficient_balance"),
        (_user(used=4, limit=5), _keys(), "quota_exhausted"),
        (_user(emailConfirmed=False), _keys(), "account_unusable"),
        (_user(balance="19532"), _keys(), "account_unusable"),
        (_user(), _keys(present=False), "ssh_material_missing"),
    ],
)
def test_preflight_refusals_happen_before_any_create(user, keys, reason):
    requests: list[tuple[str, str, object]] = []

    def request(method, path, body=None):
        requests.append((method, path, body))
        if path == "user":
            return user
        if path == "ssh-keys":
            return keys
        raise AssertionError(f"unexpected request {method} {path}")

    lifecycle = BitLaunchLifecycle(request, minimum_balance_milliusd=200)

    with pytest.raises(LifecycleRefusal, match=reason):
        lifecycle.preflight(run_tag="gha-17")

    assert not [request for request in requests if request[0] == "POST" and request[1] == "servers"]


def test_create_returns_two_distinct_run_labelled_machines_with_account_cost_and_lifetime():
    created_at = "2026-08-28T01:48:12Z"
    calls: list[tuple[str, str, object]] = []
    create_count = 0

    def request(method, path, body=None):
        nonlocal create_count
        calls.append((method, path, body))
        if path == "user":
            return _user()
        if path == "ssh-keys":
            return _keys()
        if method == "GET" and path == "servers":
            return []
        if method == "POST" and path == "servers":
            create_count += 1
            return {"id": f"server-{create_count}"}
        if method == "GET" and path.startswith("servers/"):
            index = path.rsplit("/", maxsplit=1)[1].rsplit("-", maxsplit=1)[1]
            return {
                "server": {
                    "id": f"server-{index}",
                    "ipv4": f"203.0.113.{index}",
                    "created": created_at,
                }
            }
        raise AssertionError(f"unexpected request {method} {path}")

    manifest = BitLaunchLifecycle(
        request, minimum_balance_milliusd=200, lifetime_seconds=21_600
    ).create_run(run_tag="gha-17")

    assert [(machine.id, machine.role, machine.ip) for machine in manifest.machines] == [
        ("server-1", "orchestrator", "203.0.113.1"),
        ("server-2", "target", "203.0.113.2"),
    ]
    bodies = [body for method, path, body in calls if method == "POST" and path == "servers"]
    assert [body["server"]["labels"]["role"] for body in bodies] == ["orchestrator", "target"]
    assert all(body["server"]["labels"]["run"] == "gha-17" for body in bodies)
    assert manifest.lifetime_seconds == 21_600
    assert [machine.hourly_cost_cents for machine in manifest.machines] == [42, 42]
    assert all(
        machine.created_at == created_at and machine.observed_at for machine in manifest.machines
    )
    manifest_json = manifest.to_json()
    assert "key-1" not in manifest_json
    assert "labels" not in manifest_json
    assert [path for _method, path, _body in calls if path == "user"] == ["user"]


def test_create_failure_recovers_every_machine_by_exact_run_tag():
    deleted: list[str] = []
    inventory_reads = 0

    def request(method, path, body=None):
        nonlocal inventory_reads
        if path == "user":
            return _user()
        if path == "ssh-keys":
            return _keys()
        if method == "GET" and path == "servers":
            inventory_reads += 1
            if inventory_reads == 1:
                return []
            return [
                {"id": "one", "name": "codegen-stand-gha-17-orchestrator"},
                {"id": "two", "name": "codegen-stand-gha-17-target"},
                {"id": "three", "name": "codegen-stand-gha-170-target"},
            ]
        if method == "POST" and path == "servers":
            return {"id": "one"}
        if method == "GET" and path == "servers/one":
            raise LifecycleRefusal("machine_not_ready")
        if method == "DELETE":
            deleted.append(path)
            return None
        raise AssertionError(f"unexpected request {method} {path}")

    with pytest.raises(LifecycleRefusal, match="machine_not_ready"):
        BitLaunchLifecycle(request).create_run(run_tag="gha-17")

    assert deleted == ["servers/one", "servers/two"]


def test_second_create_failure_recovers_both_exactly_tagged_resources():
    deleted: list[str] = []
    inventory_reads = 0
    create_calls = 0

    def request(method, path, body=None):
        nonlocal inventory_reads, create_calls
        if path == "user":
            return _user()
        if path == "ssh-keys":
            return _keys()
        if method == "GET" and path == "servers":
            inventory_reads += 1
            if inventory_reads == 1:
                return []
            return [
                {"id": "one", "name": "codegen-stand-gha-17-orchestrator"},
                {"id": "two", "name": "codegen-stand-gha-17-target"},
                {"id": "three", "name": "codegen-stand-gha-170-target"},
            ]
        if method == "POST" and path == "servers":
            create_calls += 1
            if create_calls == 1:
                return {"id": "one"}
            return {"id": "two"}
        if method == "GET" and path == "servers/one":
            return {
                "server": {"id": "one", "ipv4": "203.0.113.1", "created": "2026-08-28T00:00:00Z"}
            }
        if method == "GET" and path == "servers/two":
            raise LifecycleRefusal("machine_not_ready")
        if method == "DELETE":
            deleted.append(path)
            return None
        raise AssertionError(f"unexpected request {method} {path}")

    with pytest.raises(LifecycleRefusal, match="machine_not_ready"):
        BitLaunchLifecycle(request).create_run(run_tag="gha-17")

    assert deleted == ["servers/one", "servers/two"]


def test_run_ceiling_refuses_before_either_create():
    calls: list[tuple[str, str, object]] = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if path == "user":
            return _user()
        if path == "ssh-keys":
            return _keys()
        if path == "servers":
            return [
                {"id": "one", "name": "codegen-stand-gha-17-orchestrator"},
                {"id": "two", "name": "codegen-stand-gha-17-target"},
            ]
        raise AssertionError(f"unexpected request {method} {path}")

    with pytest.raises(LifecycleRefusal, match="resource_ceiling_exhausted"):
        BitLaunchLifecycle(request).create_run(run_tag="gha-17")

    assert not [request for request in calls if request[0] == "POST"]


def test_cleanup_selects_only_exactly_tagged_resources():
    deleted: list[str] = []

    def request(method, path, body=None):
        if method == "GET" and path == "servers":
            return [
                {"id": "one", "name": "codegen-stand-gha-17-orchestrator"},
                {"id": "two", "name": "codegen-stand-gha-17-target"},
                {"id": "three", "name": "codegen-stand-gha-170-target"},
                {"id": "four", "name": "unrelated"},
            ]
        if method == "DELETE":
            deleted.append(path)
            return None
        raise AssertionError(f"unexpected request {method} {path}")

    assert BitLaunchLifecycle(request).cleanup_run(run_tag="gha-17") == ["one", "two"]
    assert deleted == ["servers/one", "servers/two"]


def test_cleanup_observation_records_exact_selection_and_zero_post_cleanup_usage():
    inventory_reads = 0

    def request(method, path, body=None):
        nonlocal inventory_reads
        if path == "servers":
            inventory_reads += 1
            if inventory_reads == 1:
                return [
                    {"id": "one", "name": "codegen-stand-gha-17-orchestrator"},
                    {"id": "two", "name": "codegen-stand-gha-17-target"},
                    {"id": "other", "name": "codegen-stand-gha-170-target"},
                ]
            return [{"id": "other", "name": "codegen-stand-gha-170-target"}]
        if method == "DELETE":
            return None
        if path == "user":
            return _user(used=0, limit=5)
        raise AssertionError(f"unexpected request {method} {path}")

    observation = BitLaunchLifecycle(request).cleanup_observation(run_tag="gha-17")

    assert observation["status"] == "verified"
    assert observation["selected_ids"] == ["one", "two"]
    assert observation["deleted_ids"] == ["one", "two"]
    assert observation["remaining_ids"] == []
    assert observation["servers_used"] == 0


def test_cleanup_observation_fails_closed_when_post_cleanup_account_is_not_empty():
    inventory_reads = 0

    def request(method, path, body=None):
        nonlocal inventory_reads
        if path == "servers":
            inventory_reads += 1
            return (
                [{"id": "one", "name": "codegen-stand-gha-17-orchestrator"}]
                if inventory_reads == 1
                else []
            )
        if method == "DELETE":
            return None
        if path == "user":
            return _user(used=1, limit=5)
        raise AssertionError(f"unexpected request {method} {path}")

    observation = BitLaunchLifecycle(request).cleanup_observation(run_tag="gha-17")

    assert observation["status"] == "incomplete"
    assert observation["servers_used"] == 1
    assert "post_cleanup_servers_used_not_zero" in observation["errors"]


def test_ttl_selection_refuses_untagged_and_keeps_young_resources():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    old = (now - timedelta(hours=3)).isoformat()
    young = (now - timedelta(minutes=10)).isoformat()
    machines = [
        Machine("old", "orchestrator", None, old, "gha-17", created_at=old),
        Machine("young", "target", None, young, "gha-18", created_at=young),
        Machine("untagged", "target", None, old, None),
    ]

    assert [
        machine.id
        for machine in select_expired_run_machines(machines, now=now, ttl=timedelta(hours=1))
    ] == ["old"]
    assert RunTagDestructionPolicy("gha-17").allows(machines[0]) is True
    assert RunTagDestructionPolicy("gha-17").allows(machines[2]) is False


def test_ttl_selection_refuses_missing_or_malformed_creation_timestamps():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    machines = [
        Machine("missing", "orchestrator", None, now.isoformat(), "gha-17"),
        Machine("bad", "target", None, now.isoformat(), "gha-17", created_at="not-a-date"),
    ]

    for machine in machines:
        with pytest.raises(LifecycleRefusal, match="creation_timestamp_unusable"):
            select_expired_run_machines([machine], now=now, ttl=timedelta(hours=1))


def test_ttl_sweep_deletes_only_exactly_parsed_old_run_resources():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    deleted: list[str] = []

    def request(method, path, body=None):
        if method == "GET" and path == "servers":
            return [
                {
                    "id": "old",
                    "name": "codegen-stand-gha-17-orchestrator",
                    "created": "2026-08-27T20:00:00Z",
                },
                {
                    "id": "young",
                    "name": "codegen-stand-gha-18-target",
                    "created": "2026-08-27T23:50:00Z",
                },
                {
                    "id": "near",
                    "name": "codegen-stand-gha-170-target",
                    "created": "2026-08-27T20:00:00Z",
                },
                {"id": "other", "name": "unrelated", "created": "2026-08-27T20:00:00Z"},
            ]
        if method == "DELETE":
            deleted.append(path)
            return None
        raise AssertionError(f"unexpected request {method} {path}")

    assert BitLaunchLifecycle(request).sweep_expired(now=now, ttl=timedelta(hours=1)) == [
        "old",
        "near",
    ]
    assert deleted == ["servers/old", "servers/near"]


def test_module_cli_starts_from_repository_checkout_without_pythonpath():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["python3", "-m", "scripts.stand_lifecycle", "--help"],
        cwd=Path(__file__).parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
