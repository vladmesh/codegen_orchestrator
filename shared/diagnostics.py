"""Helpers for diagnostics that may cross trust boundaries."""

from __future__ import annotations

from collections.abc import Iterable
import re

from pydantic import ValidationError

_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:basic|bearer)\s+)[^\s]+")


def redact_diagnostic(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Return text safe to log or persist outside the process boundary."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _URL_USERINFO.sub(r"\g<scheme>[redacted]@", text)
    return _AUTHORIZATION.sub(r"\1[redacted]", text)


def safe_validation_errors(exc: ValidationError) -> list[dict]:
    """Return only stable validation shape, never values from untrusted input."""
    return [
        {"type": error["type"], "loc": list(error["loc"])}
        for error in exc.errors(include_url=False, include_input=False)
    ]
