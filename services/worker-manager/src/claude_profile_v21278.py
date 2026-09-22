"""Private credential-format adapter for the pinned Claude Code 2.1.278.

Only this module mirrors the private fields Claude Code stores in
`.credentials.json`. The worker profile reader owns stable I/O and health
classification separately. A CLI version bump must select/update this adapter
and pass the version gate in test_host_profile_readers.py.
"""

from dataclasses import dataclass
from datetime import datetime

from .host_profile import MetadataError, epoch_instant

CLAUDE_CODE_VERSION = "2.1.278"


class ClaudeProfileFormatError(ValueError):
    """The stored credentials do not match the pinned Claude Code format."""


@dataclass(frozen=True)
class ClaudeSessionMaterial:
    """Credential facts exposed to the stable reader without leaking private keys."""

    access_token: str | None
    refresh_token: str | None
    session_expires_at: datetime | None
    metadata_invalid: bool = False


def session_material(credentials: object) -> ClaudeSessionMaterial | None:
    """Read the refresh-capable OAuth material written by Claude Code 2.1.278.

    Unknown fields remain ignored because the CLI may add unrelated metadata.
    Missing/null/empty OAuth material is a logged-out profile, while wrong
    types fail closed as a format mismatch. `expiresAt` is access-token expiry
    in epoch milliseconds; invalid expiry metadata is reported separately from
    credential-format failure so diagnostics can remain fail-closed without
    treating usable refresh material as absent.
    """
    if not isinstance(credentials, dict):
        raise ClaudeProfileFormatError

    oauth = credentials.get("claudeAiOauth")
    if oauth is None or oauth == {}:
        return None
    if not isinstance(oauth, dict):
        raise ClaudeProfileFormatError

    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if any(
        value is not None and not isinstance(value, str) for value in (access_token, refresh_token)
    ):
        raise ClaudeProfileFormatError
    if not access_token and not refresh_token:
        return None

    expires_at = None
    metadata_invalid = False
    if oauth.get("expiresAt") is not None:
        try:
            expires_at = epoch_instant(oauth["expiresAt"], milliseconds=True)
        except MetadataError:
            metadata_invalid = True

    return ClaudeSessionMaterial(
        access_token=access_token,
        refresh_token=refresh_token,
        session_expires_at=expires_at,
        metadata_invalid=metadata_invalid,
    )
