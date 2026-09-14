"""The administrative SSH private key a managed server row holds.

The fleet key is the platform's whole reach into a managed target, so a row
whose key cannot be parsed is a target nothing can repair, provision or QA — and
it used to be discovered only when a playbook or a QA grant failed on it with
`error in libcrypto`. It is checked here instead, at the two points it crosses:
when an operator or the provisioner writes it, and when reconciliation is about
to use a stored one.

Only the format the fleet produces is supported: an unencrypted OpenSSH private
key (`ssh-keygen` output). PEM keys, passphrase-protected keys and anything else
are refused rather than guessed at, because no unlock material exists anywhere in
the platform to use them with.

Nothing here logs, returns or embeds key material in an error. The one derived
value that leaves is the public-key fingerprint, which is not a secret.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

OPENSSH_PRIVATE_KEY_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
OPENSSH_PRIVATE_KEY_END = "-----END OPENSSH PRIVATE KEY-----"


class AdminKeyRejection(StrEnum):
    """Why an administrative private key is not usable material."""

    EMPTY = "empty"
    NOT_OPENSSH = "not_openssh_private_key"
    # OpenSSH itself refuses a key file without a final newline, with exactly the
    # `error in libcrypto` a broken key produces, so a write that lost it is
    # refused rather than stored as a key that only some clients accept.
    NO_TERMINAL_NEWLINE = "no_terminal_newline"
    ENCRYPTED = "encrypted_without_supported_unlock"
    MALFORMED = "malformed"


class AdminKeyRejectedError(ValueError):
    """The key was refused. The message names the reason and never the key."""

    def __init__(self, rejection: AdminKeyRejection) -> None:
        super().__init__(f"administrative SSH private key rejected: {rejection.value}")
        self.rejection = rejection


@dataclass(frozen=True)
class AdminPrivateKey:
    """A parsed key: the canonical text to store or use, and its public fingerprint."""

    text: str = field(repr=False)
    fingerprint: str


def normalize_admin_private_key(raw: str | None) -> AdminPrivateKey:
    """Check a key being written to a managed server row and return its canonical form.

    CRLF line endings are normalized and surrounding blank space is dropped, but a
    missing terminal newline is refused: it is the shape a truncated paste or an
    environment variable that ate the last line leaves behind.
    """
    if raw is None or not raw.strip():
        raise AdminKeyRejectedError(AdminKeyRejection.EMPTY)
    text = raw.replace("\r\n", "\n")
    _require_openssh_armour(text)
    if not text.endswith("\n"):
        raise AdminKeyRejectedError(AdminKeyRejection.NO_TERMINAL_NEWLINE)
    return _parse(text.strip() + "\n")


def validate_stored_admin_private_key(stored: str | None) -> AdminPrivateKey:
    """Check a key already on a row before anything connects with it.

    The terminal newline is not required here: every consumer writes the key to
    a file with one appended, so its absence on an existing row is not what makes
    that row unusable. Whether the material parses is.
    """
    if stored is None or not stored.strip():
        raise AdminKeyRejectedError(AdminKeyRejection.EMPTY)
    text = stored.replace("\r\n", "\n")
    _require_openssh_armour(text)
    return _parse(text.strip() + "\n")


def _require_openssh_armour(text: str) -> None:
    stripped = text.strip()
    if not (
        stripped.startswith(OPENSSH_PRIVATE_KEY_BEGIN)
        and stripped.endswith(OPENSSH_PRIVATE_KEY_END)
    ):
        raise AdminKeyRejectedError(AdminKeyRejection.NOT_OPENSSH)


def _parse(text: str) -> AdminPrivateKey:
    try:
        key = serialization.load_ssh_private_key(text.encode(), password=None)
    except TypeError:
        # cryptography's answer for a passphrase-protected key given no password.
        raise AdminKeyRejectedError(AdminKeyRejection.ENCRYPTED) from None
    except (ValueError, UnsupportedAlgorithm):
        raise AdminKeyRejectedError(AdminKeyRejection.MALFORMED) from None
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    blob = base64.b64decode(public.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return AdminPrivateKey(text=text, fingerprint=f"SHA256:{digest}")
