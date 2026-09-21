"""Real administrative key material for tests that create managed server rows.

The API parses a managed server's key before it stores the row, so a test that
needs a managed row needs a key that is a key, not a placeholder string.
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def fleet_private_key() -> str:
    """A fresh unencrypted OpenSSH private key, as the fleet's `ssh-keygen` writes one."""
    text = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return text if text.endswith("\n") else text + "\n"
