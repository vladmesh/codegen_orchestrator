"""Unit tests for SpawnResult dataclass.

Regression tests for commit 2fd04e2 - Developer node dataclass access errors.
Tests ensure SpawnResult is always treated as a dataclass, not a dict.
"""

from dataclasses import is_dataclass

import pytest

from src.clients.worker_spawner import SpawnResult


def test_spawn_result_is_dataclass():
    """Verify SpawnResult is a dataclass."""
    assert is_dataclass(SpawnResult)
    assert hasattr(SpawnResult, "__dataclass_fields__")


def test_spawn_result_success_attribute_access():
    """Test accessing success attribute (not .get('success')).

    Regression test: Previously code used .get() which doesn't work on dataclasses.
    """
    result = SpawnResult(
        request_id="test-123",
        success=True,
        exit_code=0,
        output="Success",
    )

    # Should access as attribute, not dict
    assert result.success is True
    assert result.exit_code == 0

    # Should NOT have dict methods
    assert not hasattr(result, "get") or not callable(getattr(result, "get", None))


def test_spawn_result_error_message_attribute():
    """Test accessing error_message attribute (not .get('error')).

    Regression test: Code tried to use .get('error') which doesn't exist.
    Correct attribute is error_message.
    """
    result = SpawnResult(
        request_id="test-456",
        success=False,
        exit_code=1,
        output="Failed",
        error_message="Worker timed out after 600s",
    )

    # Should access error_message attribute
    assert result.error_message == "Worker timed out after 600s"


def test_spawn_result_commit_and_branch_attributes():
    """Test accessing commit_sha and branch attributes.

    Regression test: Code used to access .get('worker_id') which was wrong.
    Correct attributes are commit_sha and branch.
    """
    result = SpawnResult(
        request_id="test-789",
        success=True,
        exit_code=0,
        output="Completed",
        commit_sha="abc123def456",
        branch="main",
    )

    # Should access as attributes
    assert result.commit_sha == "abc123def456"
    assert result.branch == "main"


def test_spawn_result_optional_fields_none():
    """Test that optional fields default to None."""
    result = SpawnResult(
        request_id="test-minimal",
        success=True,
        exit_code=0,
        output="Done",
    )

    # Optional fields should be None
    # Optional fields should be None
    assert result.commit_sha is None
    assert result.branch is None
    assert result.files_changed is None
    assert result.error_message is None


def test_spawn_result_attribute_error_for_wrong_field():
    """Test that accessing non-existent fields raises AttributeError.

    This ensures we catch typos at runtime.
    """
    result = SpawnResult(
        request_id="test",
        success=True,
        exit_code=0,
        output="Done",
    )

    # Should raise AttributeError for non-existent fields
    with pytest.raises(AttributeError):
        _ = result.worker_id  # This field doesn't exist

    with pytest.raises(AttributeError):
        _ = result.error  # Should be error_message
