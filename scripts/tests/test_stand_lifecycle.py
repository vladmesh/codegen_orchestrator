from datetime import UTC, datetime, timedelta

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

    lifecycle = BitLaunchLifecycle(request, minimum_balance_cents=200)

    with pytest.raises(LifecycleRefusal, match=reason):
        lifecycle.preflight(run_tag="gha-17")

    assert not [request for request in requests if request[0] == "POST" and request[1] == "servers"]


def test_create_returns_two_distinct_run_labelled_machines_and_writes_no_secrets():
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

    manifest = BitLaunchLifecycle(request, minimum_balance_cents=200).create_run(run_tag="gha-17")

    assert [(machine.id, machine.role, machine.ip) for machine in manifest.machines] == [
        ("server-1", "orchestrator", "203.0.113.1"),
        ("server-2", "target", "203.0.113.2"),
    ]
    bodies = [body for method, path, body in calls if method == "POST" and path == "servers"]
    assert [body["server"]["labels"]["role"] for body in bodies] == ["orchestrator", "target"]
    assert all(body["server"]["labels"]["run"] == "gha-17" for body in bodies)
    assert "key-1" not in manifest.to_json()


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


def test_ttl_selection_refuses_untagged_and_keeps_young_resources():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    old = (now - timedelta(hours=3)).isoformat()
    young = (now - timedelta(minutes=10)).isoformat()
    machines = [
        Machine("old", "orchestrator", None, old, "gha-17"),
        Machine("young", "target", None, young, "gha-18"),
        Machine("untagged", "target", None, old, None),
    ]

    assert [
        machine.id
        for machine in select_expired_run_machines(machines, now=now, ttl=timedelta(hours=1))
    ] == ["old"]
    assert RunTagDestructionPolicy("gha-17").allows(machines[0]) is True
    assert RunTagDestructionPolicy("gha-17").allows(machines[2]) is False
