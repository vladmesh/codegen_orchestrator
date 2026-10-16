"""Helpers for diagnostics that may cross trust boundaries."""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
import re

from pydantic import ValidationError

_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]{0,31}://)[^\s/@]+@", re.IGNORECASE)
_TELEGRAM_BOT_API_URL = re.compile(r"(?i)(https?://api\.telegram\.org/bot)[^\s/?#]+")
_AUTHORIZATION = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer|token)\s+)[^\s\"',}\]]+"
)
_BASE64_VALUE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_-])")
_SECRET_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)


def _contains_encoded_secret(encoded: str, secrets: tuple[str, ...]) -> bool:
    """Return whether a base64 diagnostic fragment decodes to a known secret."""
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True).decode()
    except (UnicodeDecodeError, ValueError):
        return False
    return any(secret in decoded for secret in secrets)


def redact_diagnostic(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Return text safe to log or persist outside the process boundary."""
    text = str(value)
    known_secrets = tuple(secret for secret in secrets if secret)
    for secret in known_secrets:
        text = text.replace(secret, "[redacted]")
    text = _BASE64_VALUE.sub(
        lambda match: (
            "[redacted]"
            if _contains_encoded_secret(match.group(), known_secrets)
            else match.group()
        ),
        text,
    )
    text = _TELEGRAM_BOT_API_URL.sub(r"\1[redacted]", text)
    text = _URL_USERINFO.sub(r"\g<scheme>[redacted]@", text)
    return _AUTHORIZATION.sub(r"\1[redacted]", text)


def environment_secrets(environment: Mapping[str, str]) -> list[str]:
    """Return the non-empty values of the variables whose names mark them as secret."""
    return [value for name, value in environment.items() if value and _SECRET_NAME.search(name)]


def safe_validation_errors(exc: ValidationError) -> list[dict]:
    """Return only stable validation shape, never values from untrusted input."""
    return [
        {"type": error["type"], "loc": list(error["loc"])}
        for error in exc.errors(include_url=False, include_input=False)
    ]
