"""Unit tests for API models and schemas."""


def test_project_status_values():
    """Test that project status enum values are valid."""
    from src.models.project import ProjectStatus

    # Test a few key status values
    assert ProjectStatus.DRAFT == "draft"
    assert ProjectStatus.ACTIVE == "active"
    assert ProjectStatus.DEPLOYING == "deploying"

    # Ensure we have multiple statuses
    statuses = list(ProjectStatus)
    assert len(statuses) > 10


def test_server_status_values():
    """Test that server status enum values are valid."""
    from src.models.server import ServerStatus

    # Test a few key status values
    assert ServerStatus.DISCOVERED == "discovered"
    assert ServerStatus.READY == "ready"
    assert ServerStatus.IN_USE == "in_use"

    # Ensure we have multiple statuses
    statuses = list(ServerStatus)
    assert len(statuses) > 5
