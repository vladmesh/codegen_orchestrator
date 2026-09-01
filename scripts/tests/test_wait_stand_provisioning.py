import json

from scripts import wait_stand_provisioning
from shared.constants import Timeouts


def test_observer_budget_outlives_the_work_it_observes():
    assert wait_stand_provisioning.DEFAULT_TIMEOUT_SECONDS > (
        Timeouts.ACCESS_PHASE + Timeouts.PROVISIONING
    )


def test_snapshot_exposes_only_bounded_provisioning_state():
    snapshot = wait_stand_provisioning.provisioning_snapshot(
        {
            "handle": "bitlaunch-one",
            "status": "provisioning",
            "provisioning_attempts": 1,
            "provisioning_started_at": "2026-09-01T10:24:49Z",
            "last_health_check": None,
            "labels": {
                "provisioning_phase": "software_installation",
                "stand_run_tag": "gha-1-1",
                "secret": "must-not-leave-the-api",
            },
            "ssh_key": "private-key",
        },
        observed_at="2026-09-01T10:25:00+00:00",
    )

    assert snapshot == {
        "observed_at": "2026-09-01T10:25:00+00:00",
        "handle": "bitlaunch-one",
        "status": "provisioning",
        "provisioning_phase": "software_installation",
        "provisioning_attempts": 1,
        "provisioning_started_at": "2026-09-01T10:24:49Z",
        "last_health_check": None,
    }
    assert "secret" not in json.dumps(snapshot)
    assert "private-key" not in json.dumps(snapshot)


def test_terminal_provisioning_failure_is_fail_fast(monkeypatch, capsys):
    server = {
        "handle": "bitlaunch-one",
        "status": "error",
        "provisioning_attempts": 1,
        "labels": {"provisioning_phase": "software_installation"},
    }
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-key")
    monkeypatch.setattr("sys.argv", ["wait_stand_provisioning.py", "--handle", "bitlaunch-one"])
    monkeypatch.setattr(wait_stand_provisioning, "_read_server", lambda *_args: server)
    monkeypatch.setattr(
        wait_stand_provisioning.time,
        "monotonic",
        iter((0.0, 1.0)).__next__,
    )
    slept = []
    monkeypatch.setattr(wait_stand_provisioning.time, "sleep", slept.append)

    assert wait_stand_provisioning.main() == 1
    output = capsys.readouterr()
    assert '"status": "error"' in output.out
    assert "terminal failure" in output.err
    assert slept == []
