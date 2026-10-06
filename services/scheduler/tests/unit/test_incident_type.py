"""Unit tests for IncidentType enum."""

from shared.contracts.dto.incident import IncidentType


class TestIncidentType:
    """Verify IncidentType enum values."""

    def test_all_expected_values(self):
        """IncidentType should contain all expected incident types."""
        expected = {
            "server_unreachable",
            "provisioning_failed",
            "service_down",
            "resource_exhausted",
            "ssl_expiring",
            "provider_api_unavailable",
            "target_not_ready",
        }
        actual = {member.value for member in IncidentType}
        assert actual == expected
