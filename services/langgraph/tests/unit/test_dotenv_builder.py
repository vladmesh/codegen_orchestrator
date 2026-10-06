"""Unit tests for dotenv_builder module."""

import base64
import subprocess
from unittest.mock import patch

import pytest

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

    def test_every_line_is_newline_terminated(self):
        """A non-empty dotenv is a POSIX text file: its last line ends in a newline."""
        result = build_dotenv({"A_VAR": "a", "Z_VAR": "z"})
        assert result == "A_VAR=a\nZ_VAR=z\n"

    @pytest.mark.subprocess
    def test_appended_line_does_not_corrupt_last_value(self, tmp_path):
        """Regression for story-92b433c8 (2026-10-02): a generated deploy workflow
        appended `PUBLIC_BASE_URL=...` to the written `.env` with `sed` + `printf`.
        Without a final newline the append glued itself onto the alphabetically last
        key, USERS_GRANT_CAPABILITY, so the backend rejected the platform's grant
        (403 grant_rejected) on every redeploy."""
        capability = "cap-value_0123456789"
        dotenv_path = tmp_path / ".env"
        dotenv_path.write_text(
            build_dotenv({"APP_SECRET_KEY": "k", "USERS_GRANT_CAPABILITY": capability})
        )
        next_path = tmp_path / ".env.next"
        script = (
            'sed \'/^PUBLIC_BASE_URL=/d\' "$1" > "$2" && '
            "printf 'PUBLIC_BASE_URL=http://%s:%s\\n' 203.0.113.7 8020 >> \"$2\""
        )
        subprocess.run(["sh", "-c", script, "sh", str(dotenv_path), str(next_path)], check=True)

        parsed = dict(line.split("=", 1) for line in next_path.read_text().splitlines() if line)
        assert parsed["USERS_GRANT_CAPABILITY"] == capability
        assert parsed["PUBLIC_BASE_URL"] == "http://203.0.113.7:8020"

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
