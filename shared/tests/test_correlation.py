"""Tests for correlation ID binding and message context propagation."""

import structlog

from shared.log_config.correlation import (
    bind_message_context,
    get_correlation_id,
    set_correlation_id,
    unbind_message_context,
)


class TestSetCorrelationId:
    def setup_method(self):
        structlog.contextvars.clear_contextvars()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_set_and_get(self):
        set_correlation_id("test-123")
        assert get_correlation_id() == "test-123"

    def test_get_returns_none_when_unset(self):
        assert get_correlation_id() is None


class TestBindMessageContext:
    def setup_method(self):
        structlog.contextvars.clear_contextvars()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_binds_correlation_id(self):
        bind_message_context({"correlation_id": "corr-abc"})
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["correlation_id"] == "corr-abc"

    def test_binds_all_known_keys(self):
        data = {
            "correlation_id": "corr-1",
            "task_id": "task-2",
            "story_id": "story-3",
            "project_id": "proj-4",
            "request_id": "req-5",
        }
        bind_message_context(data)
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["correlation_id"] == "corr-1"
        assert ctx["task_id"] == "task-2"
        assert ctx["story_id"] == "story-3"
        assert ctx["project_id"] == "proj-4"
        assert ctx["request_id"] == "req-5"

    def test_ignores_unknown_keys(self):
        bind_message_context({"correlation_id": "corr-1", "unknown_field": "val"})
        ctx = structlog.contextvars.get_contextvars()
        assert "unknown_field" not in ctx

    def test_skips_empty_values(self):
        bind_message_context({"correlation_id": "", "task_id": None})
        ctx = structlog.contextvars.get_contextvars()
        assert "correlation_id" not in ctx
        assert "task_id" not in ctx

    def test_skips_missing_keys(self):
        bind_message_context({"some_other": "data"})
        ctx = structlog.contextvars.get_contextvars()
        assert "correlation_id" not in ctx


class TestUnbindMessageContext:
    def setup_method(self):
        structlog.contextvars.clear_contextvars()

    def teardown_method(self):
        structlog.contextvars.clear_contextvars()

    def test_removes_message_keys(self):
        bind_message_context({"correlation_id": "corr-1", "task_id": "task-2"})
        unbind_message_context()
        ctx = structlog.contextvars.get_contextvars()
        assert "correlation_id" not in ctx
        assert "task_id" not in ctx

    def test_preserves_service_binding(self):
        """Service-level bindings (set by setup_logging) must survive unbind."""
        structlog.contextvars.bind_contextvars(service="api")
        bind_message_context({"correlation_id": "corr-1"})
        unbind_message_context()
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["service"] == "api"
        assert "correlation_id" not in ctx
