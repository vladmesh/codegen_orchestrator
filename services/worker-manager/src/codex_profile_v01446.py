"""Private profile-format adapter for the pinned Codex CLI 0.144.6.

Only this module mirrors Codex's private on-disk auth schema and serde behavior.
The worker profile reader owns stable I/O, permissions, health classification,
and alerts separately. A CLI version bump must select/update this adapter and
pass the version gate in test_host_profile_readers.py.
"""

import base64
import binascii
import re

from .host_profile import (
    JSON_PARSE_FAILURE,
    MetadataError,
    ProfileInspection,
    iso_instant,
    load_json,
    logged_out,
)

CODEX_CLI_VERSION = "0.144.6"
NOT_REFRESHABLE = "Codex auth.json does not contain a refresh-capable ChatGPT session"
_NOT_SUBSCRIPTION = "Codex auth.json is not in the ChatGPT subscription auth_mode"
_FORMAT_MISMATCH = "Codex auth.json does not match the pinned Codex CLI auth format"

#: Pinned Codex (rust-v0.144.6) `AuthMode` wire values; any other value fails to load.
_CLI_AUTH_MODES = frozenset(
    {
        "apikey",
        "chatgpt",
        "chatgptAuthTokens",
        "headers",
        "agentIdentity",
        "personalAccessToken",
        "bedrockApiKey",
    }
)
_RFC3339 = re.compile(r"^\\d{4}-\\d{2}-\\d{2}[Tt ]\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?([Zz]|[+-]\\d{2}:\\d{2})$")
_BASE64URL_NO_PAD = re.compile(r"^[A-Za-z0-9_-]+$")
#: `header.payload.signature`; the pinned parser ignores any further segments.
_JWT_PARTS = 3
_CHATGPT_AUTH_MODE = "chatgpt"
#: Stored credentials pinned Codex (rust-v0.144.6) `AuthDotJson::resolved_mode`
#: prefers over ChatGPT when `auth_mode` is absent.
_NON_CHATGPT_CREDENTIALS = ("personal_access_token", "bedrock_api_key", "OPENAI_API_KEY")


def format_refusal(auth_data: dict) -> str | None:
    """Step 2: the file loads as pinned Codex `AuthDotJson`, as the CLI would load it.

    Mirrors rust-v0.144.6 `login/src/auth/storage.rs`, `login/src/token_data.rs`,
    `protocol/src/auth.rs` and `protocol/src/account.rs`. Unknown fields stay
    ignored (no `deny_unknown_fields`); `Option` fields may be absent or null;
    required fields must be present with their real JSON types; enums are
    strings. A malformed optional credential makes the whole file unusable,
    because the CLI rejects the file before it resolves the auth mode.
    Sequence (array) encodings of structs, which serde also accepts but Codex
    never writes, are refused: the only divergence, and it fails closed.
    """
    if not (
        # auth_mode: Option<AuthMode>, one of the pinned wire strings.
        _optional(auth_data, "auth_mode", lambda value: _is_str(value) and value in _CLI_AUTH_MODES)
        # OPENAI_API_KEY, personal_access_token: Option<String>.
        and _optional(auth_data, "OPENAI_API_KEY", _is_str)
        and _optional(auth_data, "personal_access_token", _is_str)
        # last_refresh: Option<DateTime<Utc>> from an RFC 3339 string with an offset.
        and _optional(auth_data, "last_refresh", _is_rfc3339)
        # agent_identity: Option<AgentIdentityStorage>, untagged Jwt(String) | Record.
        and _optional(auth_data, "agent_identity", _is_agent_identity)
        # bedrock_api_key: Option<BedrockApiKeyAuth { api_key: String, region: String }>.
        and _optional(auth_data, "bedrock_api_key", _is_bedrock_api_key)
    ):
        return _FORMAT_MISMATCH
    tokens = auth_data.get("tokens")
    # tokens: Option<TokenData>.
    if tokens is None:
        return None
    if not isinstance(tokens, dict):
        return _FORMAT_MISMATCH
    if not isinstance(tokens.get("refresh_token"), str):
        return NOT_REFRESHABLE
    if not (
        isinstance(tokens.get("access_token"), str)
        and _is_cli_id_token(tokens.get("id_token"))
        and _optional(tokens, "account_id", _is_str)
    ):
        return _FORMAT_MISMATCH
    return None


def _is_agent_identity(value: object) -> bool:
    """`AgentIdentityStorage`: any JWT string, or a complete `AgentIdentityAuthRecord`."""
    if isinstance(value, str):
        return True
    return isinstance(value, dict) and _is_agent_identity_record(value)


def _is_agent_identity_record(record: dict) -> bool:
    return (
        all(
            isinstance(record.get(name), str)
            for name in ("agent_runtime_id", "agent_private_key", "account_id", "chatgpt_user_id")
        )
        # plan_type: account `PlanType`, lowercase names with `#[serde(other)] Unknown`,
        # so any string loads and nothing else does.
        and isinstance(record.get("plan_type"), str)
        and isinstance(record.get("chatgpt_account_is_fedramp"), bool)
        # email: Option<String> (empty becomes None); task_id: Option<String>.
        and _optional(record, "email", _is_str)
        and _optional(record, "task_id", _is_str)
    )


def parse_json(raw: str | bytes) -> object:
    """The single JSON trust boundary for `auth.json` and decoded JWT claims.

    Delegates to the total `load_json` in pinned serde_json mode and returns
    the parsed value or `JSON_PARSE_FAILURE`; it never raises.
    """
    return load_json(raw, pinned_serde_json=True)


def _optional(data: dict, name: str, valid) -> bool:
    return data.get(name) is None or bool(valid(data[name]))


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or not _RFC3339.match(value):
        return False
    try:
        iso_instant(value)
    except MetadataError:
        return False
    return True


def _is_bedrock_api_key(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("api_key"), str)
        and isinstance(value.get("region"), str)
    )


def _is_cli_id_token(value: object) -> bool:
    """Mirror pinned `TokenData.id_token` / `parse_chatgpt_jwt_claims` / `IdClaims`.

    Three non-empty dot segments (more are ignored); a canonical base64url
    no-pad payload holding a JSON object; optional string `email`; optional
    profile object with optional string `email`; optional auth object with
    optional string claims and an optional, non-null bool fedramp flag. The
    header and signature are not interpreted, exactly as in the CLI.
    """
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    if (
        len(parts) < _JWT_PARTS
        or not all(parts[:_JWT_PARTS])
        or not _BASE64URL_NO_PAD.match(parts[1])
    ):
        return False
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (binascii.Error, ValueError):
        return False
    # URL_SAFE_NO_PAD also rejects non-canonical trailing bits, which Python ignores.
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != payload:
        return False
    claims = parse_json(decoded)
    if claims is JSON_PARSE_FAILURE or not isinstance(claims, dict):
        return False
    profile = claims.get("https://api.openai.com/profile")
    auth = claims.get("https://api.openai.com/auth")
    return (
        _optional(claims, "email", _is_str)
        and (
            profile is None or (isinstance(profile, dict) and _optional(profile, "email", _is_str))
        )
        and (auth is None or _is_auth_claims(auth))
    )


def _is_auth_claims(auth: object) -> bool:
    return (
        isinstance(auth, dict)
        and all(
            _optional(auth, name, _is_str)
            for name in ("chatgpt_plan_type", "chatgpt_user_id", "user_id", "chatgpt_account_id")
        )
        and (
            "chatgpt_account_is_fedramp" not in auth
            or isinstance(auth["chatgpt_account_is_fedramp"], bool)
        )
    )


def auth_mode_refusal(auth_data: dict) -> str | None:
    """Step 3: the CLI's authoritative mode must be the file-backed ChatGPT session.

    An explicit `auth_mode` wins; when absent the CLI resolves a stored personal
    access token, Bedrock key or OpenAI API key before ChatGPT. Any other mode
    would run on that credential instead of the subscription, so retained
    ChatGPT tokens are never interpreted for it.
    """
    mode = auth_data.get("auth_mode")
    if mode is None:
        if any(auth_data.get(name) is not None for name in _NON_CHATGPT_CREDENTIALS):
            return _NOT_SUBSCRIPTION
        return None
    return None if mode == _CHATGPT_AUTH_MODE else _NOT_SUBSCRIPTION


def session_tokens(auth_data: dict) -> tuple[str | None, str | None] | ProfileInspection:
    """Step 4: the stored ChatGPT access and refresh tokens, or the end state."""
    tokens = auth_data.get("tokens")
    if tokens is None:
        return logged_out(NOT_REFRESHABLE)
    # Step 2 proved both are strings.
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    if not access_token and not refresh_token:
        return logged_out(NOT_REFRESHABLE)
    return access_token, refresh_token
