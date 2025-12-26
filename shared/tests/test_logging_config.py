"""Tests for shared logging configuration."""

import json
import logging
import re

import pytest
import structlog

from shared.logging_config import get_logger, setup_logging


def strip_ansi(text):
    """Strip ANSI escape sequences from text."""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def parse_json_lines(output):
    """Parse output containing multiple JSON lines."""
    lines = output.strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(autouse=True)
def reset_logging():
    """Reset logging configuration before each test."""
    # Reset standard library logging
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Clear any existing structlog configuration
    structlog.reset_defaults()
    # Clear contextvars
    structlog.contextvars.clear_contextvars()
    yield
    # Cleanup after test
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


class TestLoggingSetup:
    """Test logging setup and configuration."""

    def test_setup_logging_with_defaults(self, monkeypatch):
        """Test setup_logging with default parameters."""
        # Remove env vars to test defaults
        monkeypatch.delenv("SERVICE_NAME", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        setup_logging()

        logger = structlog.get_logger()
        assert logger is not None

    def test_setup_logging_json_format(self, monkeypatch, capsys):
        """Test setup_logging with JSON format."""
        setup_logging(service_name="test_service", log_format="json", log_level="INFO")

        logger = structlog.get_logger()
        logger.info("test_event", key1="value1", key2=123)

        captured = capsys.readouterr()
        output = captured.out

        entries = parse_json_lines(output)
        # Find our event
        log_entry = next((e for e in entries if e.get("event") == "test_event"), None)
        assert log_entry is not None

        assert log_entry["service"] == "test_service"
        assert log_entry["key1"] == "value1"
        assert log_entry["key2"] == 123  # noqa: PLR2004
        assert log_entry["level"] == "info"
        assert "timestamp" in log_entry

    def test_setup_logging_console_format(self, capsys):
        """Test setup_logging with console format."""
        setup_logging(service_name="test_service", log_format="console", log_level="INFO")

        logger = structlog.get_logger()
        logger.info("test_event", key1="value1", key2=123)

        captured = capsys.readouterr()
        output = strip_ansi(captured.out)

        # Console format usually has key=value pairs or colored text
        assert "test_event" in output
        assert "key1=value1" in output
        assert "key2=123" in output
        assert "[info     ]" in output or "INFO" in output

    def test_setup_logging_from_env(self, monkeypatch, capsys):
        """Test setup_logging reads from environment variables."""
        monkeypatch.setenv("SERVICE_NAME", "env_service")
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        setup_logging()

        logger = structlog.get_logger()
        logger.debug("debug_event", test=True)

        captured = capsys.readouterr()
        entries = parse_json_lines(captured.out)
        log_entry = next((e for e in entries if e.get("event") == "debug_event"), None)

        assert log_entry is not None
        assert log_entry["service"] == "env_service"
        assert log_entry["level"] == "debug"

    def test_log_level_filtering(self, capsys):
        """Test that log level filtering works correctly."""
        setup_logging(service_name="test_service", log_format="console", log_level="WARNING")

        logger = structlog.get_logger()

        logger.info("info_event")  # Should be filtered out
        logger.warning("warning_event")  # Should be logged
        logger.error("error_event")  # Should be logged

        captured = capsys.readouterr()
        output = strip_ansi(captured.out)

        assert "info_event" not in output
        assert "warning_event" in output
        assert "error_event" in output


class TestContextInjection:
    """Test context variable injection."""

    def test_bind_contextvars(self, capsys):
        """Test binding context variables."""
        setup_logging(
            service_name="test_service",
            log_format="json",  # Use JSON to easily check fields
            log_level="INFO",
        )

        logger = structlog.get_logger()

        # Bind context
        structlog.contextvars.bind_contextvars(thread_id="thread_123", correlation_id="corr_456")

        logger.info("event_with_context")

        captured = capsys.readouterr()
        entries = parse_json_lines(captured.out)
        log_entry = next((e for e in entries if e.get("event") == "event_with_context"), None)

        assert log_entry is not None
        assert log_entry["thread_id"] == "thread_123"
        assert log_entry["correlation_id"] == "corr_456"

    def test_clear_contextvars(self, capsys):
        """Test clearing context variables."""
        setup_logging(service_name="test_service", log_format="json", log_level="INFO")

        logger = structlog.get_logger()

        # Bind and then clear context
        structlog.contextvars.bind_contextvars(thread_id="thread_123")
        structlog.contextvars.clear_contextvars()

        logger.info("event_without_context")

        captured = capsys.readouterr()
        entries = parse_json_lines(captured.out)
        log_entry = next((e for e in entries if e.get("event") == "event_without_context"), None)

        assert log_entry is not None
        assert "thread_id" not in log_entry


class TestGetLogger:
    """Test get_logger helper function."""

    def test_get_logger_without_name(self):
        """Test get_logger without explicit name."""
        setup_logging(service_name="test_service")
        logger = get_logger()
        assert logger is not None
        assert hasattr(logger, "info")

    def test_get_logger_with_name(self):
        """Test get_logger with explicit name."""
        setup_logging(service_name="test_service")
        logger = get_logger("custom.logger")
        assert logger is not None
        assert hasattr(logger, "info")


class TestErrorLogging:
    """Test error and exception logging."""

    def test_error_with_exception_info(self, capsys):
        """Test logging errors with exception information."""
        setup_logging(service_name="test_service", log_format="json", log_level="INFO")

        logger = structlog.get_logger()

        try:
            raise ValueError("Test error 12345")
        except ValueError as e:
            logger.error("error_event", error=str(e), error_type=type(e).__name__, exc_info=True)

        captured = capsys.readouterr()
        entries = parse_json_lines(captured.out)
        log_entry = next((e for e in entries if e.get("event") == "error_event"), None)

        assert log_entry is not None
        assert log_entry["error"] == "Test error 12345"
        assert log_entry["error_type"] == "ValueError"

        # Check for exception info in string representation or specific field
        # structlog with rich/json renderer puts stack trace in 'exception' by default
        assert "exception" in log_entry or "exc_info" in log_entry or "stack_info" in log_entry
