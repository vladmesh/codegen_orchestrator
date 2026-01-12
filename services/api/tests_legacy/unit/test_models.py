"""Unit tests for API models and schemas."""

# Constants for test assertions
MIN_PROJECT_STATUSES = 10  # We expect at least 10 project statuses
MIN_SERVER_STATUSES = 5  #  We expect at least 5 server statuses


def test_project_status_values():
    """Test that project status enum values are valid."""
    from shared.models.project import ProjectStatus

    # Test a few key status values
    assert ProjectStatus.DRAFT == "draft"
    assert ProjectStatus.ACTIVE == "active"
    assert ProjectStatus.DEPLOYING == "deploying"

    # Ensure we have multiple statuses
    statuses = list(ProjectStatus)
    assert len(statuses) > MIN_PROJECT_STATUSES


def test_server_status_values():
    """Test that server status enum values are valid."""
    from shared.models.server import ServerStatus

    # Test a few key status values
    assert ServerStatus.DISCOVERED == "discovered"
    assert ServerStatus.READY == "ready"
    assert ServerStatus.IN_USE == "in_use"

    # Ensure we have multiple statuses
    statuses = list(ServerStatus)
    assert len(statuses) > MIN_SERVER_STATUSES
