"""Fernet encryption for project secrets at rest.

Usage:
    from shared.crypto import encrypt_dict, decrypt_dict

    # Encrypt before saving to DB
    config["secrets"] = encrypt_dict(secrets)

    # Decrypt after reading from DB
    secrets = decrypt_dict(config.get("secrets", {}))

Requires SECRETS_ENCRYPTION_KEY env var (Fernet key).
Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os

from cryptography.fernet import Fernet


class SecretsCipher:
    """Encrypt/decrypt individual secret values using Fernet.

    Reads SECRETS_ENCRYPTION_KEY from environment on instantiation.
    Raises RuntimeError if the key is not set.
    """

    def __init__(self):
        key = os.getenv("SECRETS_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "SECRETS_ENCRYPTION_KEY is not set. "
                "Generate with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext
        return self._fernet.decrypt(ciphertext.encode()).decode()


def encrypt_dict(d: dict[str, str]) -> dict[str, str]:
    """Encrypt all values in a dict. Creates a new SecretsCipher per call."""
    if not d:
        return d
    cipher = SecretsCipher()
    return {k: cipher.encrypt(v) for k, v in d.items()}


def decrypt_dict(d: dict[str, str]) -> dict[str, str]:
    """Decrypt all values in a dict. Raises InvalidToken if any value is not valid ciphertext."""
    if not d:
        return d
    cipher = SecretsCipher()
    return {k: cipher.decrypt(v) for k, v in d.items()}
