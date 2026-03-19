"""Unit tests for SSL certificate expiry checker."""

from __future__ import annotations

from datetime import UTC, datetime
import ssl
from unittest.mock import MagicMock, patch

import pytest


class TestCheckSslExpiry:
    """Tests for check_ssl_expiry function."""

    @pytest.mark.asyncio
    async def test_valid_cert_returns_expiry_datetime(self):
        """Valid SSL cert → returns notAfter as datetime."""
        mock_cert = {"notAfter": "Mar 20 12:00:00 2027 GMT"}
        mock_conn = MagicMock()
        mock_conn.getpeercert.return_value = mock_cert

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("src.tasks.ssl_checker.ssl.create_default_context", return_value=mock_ctx),
            patch("src.tasks.ssl_checker.socket.create_connection") as mock_socket,
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)

            from src.tasks.ssl_checker import check_ssl_expiry

            result = await check_ssl_expiry("10.0.0.1", 443)

        assert result is not None
        assert isinstance(result, datetime)
        assert result.year == 2027
        assert result.month == 3
        assert result.day == 20

    @pytest.mark.asyncio
    async def test_expired_cert_returns_past_datetime(self):
        """Expired SSL cert → returns past datetime."""
        mock_cert = {"notAfter": "Jan 01 00:00:00 2020 GMT"}
        mock_conn = MagicMock()
        mock_conn.getpeercert.return_value = mock_cert

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("src.tasks.ssl_checker.ssl.create_default_context", return_value=mock_ctx),
            patch("src.tasks.ssl_checker.socket.create_connection") as mock_socket,
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)

            from src.tasks.ssl_checker import check_ssl_expiry

            result = await check_ssl_expiry("10.0.0.1", 443)

        assert result is not None
        assert result < datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_connection_failure_returns_none(self):
        """Connection failure → returns None."""
        with patch(
            "src.tasks.ssl_checker.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ):
            from src.tasks.ssl_checker import check_ssl_expiry

            result = await check_ssl_expiry("10.0.0.1", 443)

        assert result is None

    @pytest.mark.asyncio
    async def test_ssl_error_returns_none(self):
        """SSL handshake failure → returns None."""
        with (
            patch("src.tasks.ssl_checker.socket.create_connection") as mock_socket,
            patch(
                "src.tasks.ssl_checker.ssl.create_default_context",
                side_effect=ssl.SSLError("handshake failed"),
            ),
        ):
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock(return_value=False)

            from src.tasks.ssl_checker import check_ssl_expiry

            result = await check_ssl_expiry("10.0.0.1", 443)

        assert result is None
