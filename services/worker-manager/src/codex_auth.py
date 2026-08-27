"""Fail-fast validation for the dedicated Codex host-session profile."""

import json
from pathlib import Path
import stat
import tomllib


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def validate_codex_host_session(profile_path: str | None) -> None:
    """Validate the file-backed ChatGPT session without exposing its contents."""
    if not profile_path:
        raise RuntimeError(
            "HOST_CODEX_HOME is required for Codex auth_mode=host_session; "
            "configure a dedicated profile created with codex login --device-auth"
        )

    profile = Path(profile_path)
    if not profile.is_dir():
        raise RuntimeError("HOST_CODEX_HOME is not an existing directory")
    if _mode(profile) != 0o700:
        raise RuntimeError("HOST_CODEX_HOME must have mode 0700")

    auth_path = profile / "auth.json"
    if not auth_path.is_file() or auth_path.stat().st_size == 0:
        raise RuntimeError("Codex host session is missing a non-empty auth.json")
    if _mode(auth_path) != 0o600:
        raise RuntimeError("Codex auth.json must have mode 0600")
    try:
        auth_data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex auth.json is unreadable or invalid JSON") from exc
    if not isinstance(auth_data, dict) or not auth_data:
        raise RuntimeError("Codex auth.json does not contain a cached session")
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(name), str) and tokens[name] for name in ("access_token", "refresh_token")
    ):
        raise RuntimeError("Codex auth.json does not contain a refresh-capable ChatGPT session")

    config_path = profile / "config.toml"
    if not config_path.is_file():
        raise RuntimeError("Codex host session is missing config.toml")
    if _mode(config_path) != 0o600:
        raise RuntimeError("Codex config.toml must have mode 0600")
    try:
        config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError("Codex config.toml is unreadable or invalid TOML") from exc
    if config.get("cli_auth_credentials_store") != "file":
        raise RuntimeError('Codex config.toml must set cli_auth_credentials_store = "file"')
