"""The administrative key a managed server row may hold, checked before it is stored or used."""

from pathlib import Path
import subprocess

import pytest

from shared.ssh_keys import (
    AdminKeyRejectedError,
    AdminKeyRejection,
    normalize_admin_private_key,
    validate_stored_admin_private_key,
)


def _keygen(tmp_path: Path, name: str, *args: str) -> tuple[str, str]:
    """A real `ssh-keygen` pair: the private key text and `ssh-keygen -l` fingerprint."""
    path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", *args, "-f", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    listed = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(path)], check=True, capture_output=True, text=True
    )
    return path.read_text(), listed.stdout.split()[1]


@pytest.fixture
def fleet_key(tmp_path) -> tuple[str, str]:
    return _keygen(tmp_path, "fleet", "-t", "ed25519", "-N", "")


def test_a_fleet_key_is_accepted_with_the_fingerprint_openssh_reports(fleet_key):
    text, fingerprint = fleet_key

    key = normalize_admin_private_key(text)

    assert key.text == text
    assert key.fingerprint == fingerprint


def test_crlf_and_surrounding_blank_space_are_normalized(fleet_key):
    text, fingerprint = fleet_key

    key = normalize_admin_private_key("\n" + text.replace("\n", "\r\n") + "\r\n")

    assert key.text == text
    assert key.fingerprint == fingerprint


@pytest.mark.parametrize("raw", [None, "", "   \n"])
def test_an_empty_key_is_refused(raw):
    with pytest.raises(AdminKeyRejectedError) as refused:
        normalize_admin_private_key(raw)

    assert refused.value.rejection is AdminKeyRejection.EMPTY


def test_a_key_without_its_terminal_newline_is_refused_on_write(fleet_key):
    text, _ = fleet_key

    with pytest.raises(AdminKeyRejectedError) as refused:
        normalize_admin_private_key(text.rstrip("\n"))

    assert refused.value.rejection is AdminKeyRejection.NO_TERMINAL_NEWLINE


def test_a_truncated_key_is_refused_and_the_error_never_carries_it(fleet_key):
    text, _ = fleet_key
    lines = text.splitlines()
    truncated = "\n".join([lines[0], lines[1][:20], lines[-1]]) + "\n"

    with pytest.raises(AdminKeyRejectedError) as refused:
        normalize_admin_private_key(truncated)

    assert refused.value.rejection is AdminKeyRejection.MALFORMED
    assert lines[1][:20] not in str(refused.value)
    assert refused.value.__cause__ is None


def test_a_passphrase_protected_key_is_refused(tmp_path):
    text, _ = _keygen(tmp_path, "locked", "-t", "ed25519", "-N", "a passphrase")

    with pytest.raises(AdminKeyRejectedError) as refused:
        normalize_admin_private_key(text)

    assert refused.value.rejection is AdminKeyRejection.ENCRYPTED


def test_a_pem_key_is_not_a_supported_format(tmp_path):
    text, _ = _keygen(tmp_path, "pem", "-t", "rsa", "-b", "2048", "-m", "PEM", "-N", "")

    with pytest.raises(AdminKeyRejectedError) as refused:
        normalize_admin_private_key(text)

    assert refused.value.rejection is AdminKeyRejection.NOT_OPENSSH


def test_a_stored_key_is_validated_without_requiring_the_newline(fleet_key):
    """Consumers append the newline when they write the key file; parseability decides."""
    text, fingerprint = fleet_key

    assert validate_stored_admin_private_key(text.rstrip("\n")).fingerprint == fingerprint


def test_a_stored_key_that_does_not_parse_is_refused(fleet_key):
    text, _ = fleet_key
    broken = text.replace(text.splitlines()[2], "not-base64!!", 1)

    with pytest.raises(AdminKeyRejectedError) as refused:
        validate_stored_admin_private_key(broken)

    assert refused.value.rejection is AdminKeyRejection.MALFORMED


def test_the_parsed_key_does_not_print_its_material(fleet_key):
    text, _ = fleet_key

    assert text.splitlines()[1] not in repr(normalize_admin_private_key(text))
