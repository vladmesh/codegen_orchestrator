"""Build and encode .env content for deployment secrets."""

import base64

import structlog

logger = structlog.get_logger()

# GitHub Actions secrets have a 48KB limit
_MAX_SECRET_BYTES = 48 * 1024


def _needs_quoting(value: str) -> bool:
    """Check if a value needs double-quoting in a dotenv file."""
    return any(c in value for c in (" ", "=", '"', "'", "\n", "#"))


def build_dotenv(secrets: dict[str, str]) -> str:
    """Build dotenv file content from a secrets dict.

    Keys are sorted for deterministic output. Values containing
    special characters are double-quoted.
    """
    lines = []
    for key in sorted(secrets):
        value = secrets[key]
        if _needs_quoting(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def encode_dotenv(dotenv_content: str) -> str:
    """Base64-encode dotenv content for storage as a GitHub secret.

    Logs a warning if the encoded content exceeds 48KB (GitHub Actions limit).
    """
    raw = dotenv_content.encode("utf-8")
    if len(raw) > _MAX_SECRET_BYTES:
        logger.warning(
            "dotenv_exceeds_secret_limit",
            size_bytes=len(raw),
            limit_bytes=_MAX_SECRET_BYTES,
        )
    return base64.b64encode(raw).decode("utf-8")
