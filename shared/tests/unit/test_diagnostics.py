"""Regression tests for diagnostics that cross process boundaries."""

import base64

from shared.diagnostics import redact_diagnostic

TELEGRAM_TOKEN = "123456789:AA-redaction-canary-token"  # noqa: S105
USERS_GRANT_CAPABILITY = "users-grant-redaction-canary"


def test_redact_diagnostic_elides_telegram_urls_headers_and_encoded_dotenv() -> None:
    dotenv = base64.b64encode(
        (
            f"TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN}\n"
            f"USERS_GRANT_CAPABILITY={USERS_GRANT_CAPABILITY}\n"
        ).encode()
    ).decode()
    diagnostic = (
        f"request https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe failed; "
        "Authorization: Bearer authorization-redaction-canary; "
        f"DOTENV={dotenv}"
    )

    redacted = redact_diagnostic(diagnostic, secrets=(TELEGRAM_TOKEN, USERS_GRANT_CAPABILITY))

    assert TELEGRAM_TOKEN not in redacted
    assert USERS_GRANT_CAPABILITY not in redacted
    assert dotenv not in redacted
    assert "authorization-redaction-canary" not in redacted
    assert "https://api.telegram.org/bot[redacted]/getMe" in redacted


def test_redact_diagnostic_recognizes_telegram_endpoint_without_a_known_secret() -> None:
    endpoint = "https://api.telegram.org/bot987654321:AA-url-only-canary/sendMessage"

    redacted = redact_diagnostic(f"provider rejected {endpoint}")

    assert "987654321:AA-url-only-canary" not in redacted
    assert redacted.endswith("https://api.telegram.org/bot[redacted]/sendMessage")
