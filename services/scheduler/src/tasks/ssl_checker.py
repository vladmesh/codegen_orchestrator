"""SSL certificate expiry checker.

Connects to host:port via SSL socket, extracts the certificate's notAfter
date, and returns it as a datetime. Runs blocking socket call in an executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import socket
import ssl

import structlog

logger = structlog.get_logger()

SSL_CHECK_TIMEOUT = 5


def _get_cert_expiry(host: str, port: int) -> datetime | None:
    """Blocking call: connect via SSL and extract cert expiry date."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with socket.create_connection((host, port), timeout=SSL_CHECK_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert or "notAfter" not in cert:
                    return None
                expiry_str = cert["notAfter"]
                return datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except Exception:
        logger.debug("ssl_check_failed", host=host, port=port)
        return None


async def check_ssl_expiry(host: str, port: int) -> datetime | None:
    """Check SSL certificate expiry for host:port.

    Returns the cert's notAfter datetime (UTC), or None on failure.
    Runs the blocking socket call in an executor.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_cert_expiry, host, port)
