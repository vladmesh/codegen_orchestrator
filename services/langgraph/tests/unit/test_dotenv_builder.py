"""Unit tests for dotenv_builder module."""

import base64
from unittest.mock import patch

from src.subgraphs.devops.dotenv_builder import build_dotenv, encode_dotenv


class TestBuildDotenv:
    def test_basic_format(self):
        secrets = {"DB_HOST": "localhost", "DB_PORT": "5432"}
        result = build_dotenv(secrets)
        assert "DB_HOST=localhost" in result
        assert "DB_PORT=5432" in result

    def test_values_with_special_characters(self):
        secrets = {"PASSWORD": "p@ss w0rd=yes", "SIMPLE": "abc"}
        result = build_dotenv(secrets)
        # Values with spaces or = should be quoted
        assert 'PASSWORD="p@ss w0rd=yes"' in result
        assert "SIMPLE=abc" in result

    def test_empty_dict(self):
        result = build_dotenv({})
        assert result == ""

    def test_sorted_output(self):
        secrets = {"Z_VAR": "z", "A_VAR": "a", "M_VAR": "m"}
        result = build_dotenv(secrets)
        lines = result.strip().split("\n")
        assert lines[0] == "A_VAR=a"
        assert lines[1] == "M_VAR=m"
        assert lines[2] == "Z_VAR=z"


class TestEncodeDotenv:
    def test_base64_roundtrip(self):
        secrets = {"KEY": "value", "OTHER": "stuff"}
        dotenv = build_dotenv(secrets)
        encoded = encode_dotenv(dotenv)
        decoded = base64.b64decode(encoded).decode("utf-8")
        assert decoded == dotenv

    def test_large_dotenv_warns(self):
        """Content > 48KB should log a warning but not raise."""
        large_content = "X" * 50_000
        with patch("src.subgraphs.devops.dotenv_builder.logger") as mock_logger:
            result = encode_dotenv(large_content)
            mock_logger.warning.assert_called_once()
            # Should still return encoded content
            assert base64.b64decode(result).decode("utf-8") == large_content
