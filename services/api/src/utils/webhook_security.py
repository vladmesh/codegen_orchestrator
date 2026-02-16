"""GitHub webhook signature verification.

Verifies HMAC-SHA256 signatures sent by GitHub in the X-Hub-Signature-256 header.
"""

import hashlib
import hmac


def verify_github_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the GitHub webhook HMAC-SHA256 signature.

    Args:
        payload_body: Raw request body bytes.
        signature_header: Value of X-Hub-Signature-256 header (e.g. "sha256=abc123...").
        secret: Webhook secret configured in GitHub App settings.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = (
        "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected_signature, signature_header)
