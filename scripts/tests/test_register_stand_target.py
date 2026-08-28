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
    assert payload["labels"] == {
        "contour": "stand",
        "provider": "bitlaunch",
        "provider_id": "71234",
        "stand_run_tag": "gha-41-1",
        "stand_role": "target",
    }
    assert "provisioning_phase" not in payload["labels"]
