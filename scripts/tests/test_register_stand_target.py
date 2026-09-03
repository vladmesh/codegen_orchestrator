import json

from scripts import register_bitlaunch_target
from scripts.register_bitlaunch_target import build_target_payload
from shared.contracts.dto.server import ServerCreate


def test_dynamic_target_registration_is_pending_and_carries_only_public_run_identity():
    payload = build_target_payload(
        target_id="6a920e74c9c98a452507b09b",
        target_ip="203.0.113.19",
        run_tag="gha-41-1",
        ssh_private_key="private-key-material",
    )

    assert payload["handle"] == "bitlaunch-6a920e74c9c98a452507b09b"
    assert payload["status"] == "pending_setup"
    assert payload["ssh_key"] == "private-key-material"
    assert payload["capacity_cpu"] == 4
    assert payload["capacity_ram_mb"] == 8192
    assert payload["capacity_disk_mb"] == 153600
    assert payload["labels"] == {
        "contour": "stand",
        "provider": "bitlaunch",
        "provider_id": "6a920e74c9c98a452507b09b",
        "stand_run_tag": "gha-41-1",
        "stand_role": "target",
    }
    assert "provisioning_phase" not in payload["labels"]


def test_the_stand_row_and_a_production_row_name_the_same_administrative_account():
    """One administrative account model for both contours, pinned.

    `servers.ssh_user` is the account the fleet key opens, and the QA grant
    writes *another* account's `authorized_keys` over that connection — which
    only an administrative account can do. A stand row that named `deploy`
    instead blocked three paid runs with a missing QA seat that was there all
    along, and the line that introduced it arrived inside a bundle about key
    files and capacity. This test is what makes that recur visibly: the
    production row is built the way `server_sync` builds one, naming no
    `ssh_user` at all, and the stand payload has to agree with it.
    """
    production = ServerCreate(
        handle="vps-4242",
        host="vps-4242.example.net",
        public_ip="203.0.113.4",
    )
    payload = build_target_payload(
        target_id="6a920e74c9c98a452507b09b",
        target_ip="203.0.113.19",
        run_tag="gha-41-1",
        ssh_private_key="private-key-material",
    )

    assert payload["ssh_user"] == production.ssh_user
    assert payload["ssh_user"] == "root"


def test_registration_reads_the_multiline_creation_key_from_its_protected_file(
    monkeypatch, tmp_path
):
    registration_input = tmp_path / "target.json"
    registration_input.write_text(
        json.dumps(
            {
                "target_id": "6a920e74c9c98a452507b09b",
                "target_ip": "203.0.113.19",
                "run_tag": "gha-41-1",
            }
        )
    )
    key_file = tmp_path / "target.key"
    key_file.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nmultiline\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    captured = {}
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-key")
    monkeypatch.setattr(
        register_bitlaunch_target,
        "_request",
        lambda url, key, payload: captured.update(url=url, key=key, payload=payload) or payload,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "register_bitlaunch_target.py",
            "--input",
            str(registration_input),
            "--ssh-private-key-file",
            str(key_file),
        ],
    )

    assert register_bitlaunch_target.main() == 0
    assert captured["payload"]["ssh_key"] == key_file.read_text()
