"""Unit tests for shared.crypto module."""

from unittest.mock import patch

from cryptography.fernet import Fernet


class TestSecretsCipher:
    """Tests for SecretsCipher class."""

    def setup_method(self):
        self.test_key = Fernet.generate_key().decode()

    def _make_cipher(self):
        from shared.crypto import SecretsCipher

        with patch.dict("os.environ", {"SECRETS_ENCRYPTION_KEY": self.test_key}):
            return SecretsCipher()

    def test_encrypt_decrypt_roundtrip(self):
        cipher = self._make_cipher()
        original = "my-secret-value-123"
        encrypted = cipher.encrypt(original)
        decrypted = cipher.decrypt(encrypted)
        assert decrypted == original

    def test_encrypt_produces_fernet_token(self):
        cipher = self._make_cipher()
        encrypted = cipher.encrypt("test-value")
        assert encrypted.startswith("gAAAAA")

    def test_different_values_different_ciphertexts(self):
        cipher = self._make_cipher()
        enc1 = cipher.encrypt("value-one")
        enc2 = cipher.encrypt("value-two")
        assert enc1 != enc2

    def test_same_value_different_ciphertexts(self):
        """Fernet uses timestamp + random IV, so same plaintext produces different ciphertexts."""
        cipher = self._make_cipher()
        enc1 = cipher.encrypt("same-value")
        enc2 = cipher.encrypt("same-value")
        assert enc1 != enc2

    def test_missing_key_raises_runtime_error(self):
        import pytest

        from shared.crypto import SecretsCipher

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="SECRETS_ENCRYPTION_KEY"):
                SecretsCipher()

    def test_decrypt_plaintext_value_returns_as_is(self):
        """Graceful degradation: non-Fernet values are returned as-is."""
        cipher = self._make_cipher()
        plaintext = "just-a-plain-string"
        result = cipher.decrypt(plaintext)
        assert result == plaintext

    def test_decrypt_empty_string(self):
        cipher = self._make_cipher()
        result = cipher.decrypt("")
        assert result == ""


class TestEncryptDecryptDict:
    """Tests for encrypt_dict / decrypt_dict module-level helpers."""

    def setup_method(self):
        self.test_key = Fernet.generate_key().decode()

    def test_encrypt_dict_decrypt_dict_roundtrip(self):
        from shared.crypto import decrypt_dict, encrypt_dict

        original = {"DB_URL": "postgres://...", "API_KEY": "sk-123"}
        with patch.dict("os.environ", {"SECRETS_ENCRYPTION_KEY": self.test_key}):
            encrypted = encrypt_dict(original)
            decrypted = decrypt_dict(encrypted)

        assert decrypted == original
        # All encrypted values should be Fernet tokens
        for v in encrypted.values():
            assert v.startswith("gAAAAA")

    def test_encrypt_dict_empty(self):
        from shared.crypto import encrypt_dict

        with patch.dict("os.environ", {"SECRETS_ENCRYPTION_KEY": self.test_key}):
            result = encrypt_dict({})
        assert result == {}

    def test_decrypt_dict_mixed_plaintext_and_encrypted(self):
        """Migration scenario: some values are encrypted, some are plaintext."""
        from shared.crypto import decrypt_dict, encrypt_dict

        with patch.dict("os.environ", {"SECRETS_ENCRYPTION_KEY": self.test_key}):
            encrypted_val = encrypt_dict({"KEY": "encrypted-secret"})["KEY"]

            mixed = {
                "OLD_PLAIN": "legacy-plaintext-value",
                "NEW_ENC": encrypted_val,
            }
            decrypted = decrypt_dict(mixed)

        assert decrypted["OLD_PLAIN"] == "legacy-plaintext-value"
        assert decrypted["NEW_ENC"] == "encrypted-secret"
