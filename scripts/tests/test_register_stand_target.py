import json

from scripts import register_bitlaunch_target
from scripts.register_bitlaunch_target import build_target_payload


def test_dynamic_target_registration_is_pending_and_carries_only_public_run_identity():
    payload = build_target_payload(
        target_id="71234",
        target_ip="203.0.113.19",
        run_tag="gha-41-1",
        ssh_private_key="private-key-material",
    )

    assert payload["handle"] == "bitlaunch-71234"
    assert payload["status"] == "pending_setup"
    assert payload["ssh_key"] == "private-key-material"
    assert payload["ssh_user"] == "deploy"
    assert payload["capacity_cpu"] == 4
    assert payload["capacity_ram_mb"] == 8192
    assert payload["capacity_disk_mb"] == 153600
    assert payload["labels"] == {
        "contour": "stand",
        "provider": "bitlaunch",
        "provider_id": "71234",
        "stand_run_tag": "gha-41-1",
        "stand_role": "target",
    }
    assert "provisioning_phase" not in payload["labels"]


def test_registration_reads_the_multiline_creation_key_from_its_protected_file(
    monkeypatch, tmp_path
):
    registration_input = tmp_path / "target.json"
    registration_input.write_text(
        json.dumps({"target_id": "71234", "target_ip": "203.0.113.19", "run_tag": "gha-41-1"})
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
