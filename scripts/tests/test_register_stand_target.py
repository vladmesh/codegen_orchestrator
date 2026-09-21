import io
import json
import urllib.error

from scripts import register_bitlaunch_target
from scripts.register_bitlaunch_target import build_target_payload
from shared.contracts.dto.server import ServerCreate

KEY_BODY = "b3BlbnNzaC1rZXktdjEAAAAAsecretmaterialthatmustnotbeprinted"


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


def _run_against_refusal(monkeypatch, tmp_path, *, status: int, body: bytes) -> int:
    """Drive `main` against an API that refuses the registration with `body`."""
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
    key_file.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{KEY_BODY}\n")

    def _refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, status, "Unprocessable Entity", {}, io.BytesIO(body)
        )

    monkeypatch.setenv("INTERNAL_API_KEY", "internal-key-not-for-the-log")
    monkeypatch.setattr(register_bitlaunch_target.urllib.request, "urlopen", _refuse)
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
    return register_bitlaunch_target.main()


def test_an_api_refusal_is_reported_as_its_reason_and_never_as_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """What run 35380550303 could not tell anyone: why the API said no.

    The step's only output was `HTTPError: HTTP Error 422`, so eight minutes and
    a machine pair bought a status code. The reason the API states is the whole
    diagnosis, and it is one line.
    """
    exit_code = _run_against_refusal(
        monkeypatch,
        tmp_path,
        status=422,
        body=json.dumps({"detail": "ssh_key rejected: no_terminal_newline"}).encode(),
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert "422" in lines[0]
    assert "ssh_key rejected: no_terminal_newline" in lines[0]
    assert "bitlaunch-6a920e74c9c98a452507b09b" in lines[0]
    assert KEY_BODY not in captured.err
    assert "OPENSSH PRIVATE KEY" not in captured.err
    assert "internal-key-not-for-the-log" not in captured.err


def test_a_refusal_body_that_quotes_the_request_back_is_named_rather_than_printed(
    monkeypatch, tmp_path, capsys
):
    """FastAPI's request-validation body carries the rejected input, and the
    input here is the creation key. Only a textual `detail` is repeated."""
    exit_code = _run_against_refusal(
        monkeypatch,
        tmp_path,
        status=422,
        body=json.dumps(
            {"detail": [{"loc": ["body", "ssh_key"], "msg": "bad", "input": KEY_BODY}]}
        ).encode(),
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert KEY_BODY not in captured.err
    assert "422" in captured.err
