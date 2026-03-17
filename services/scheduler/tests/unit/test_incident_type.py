"""Unit tests for IncidentType enum."""

from shared.models.incident import IncidentType


class TestIncidentType:
    """Verify IncidentType enum values."""

    def test_has_five_values(self):
        """IncidentType should have exactly 5 values."""
        assert len(IncidentType) == 5

    def test_ssl_expiring_exists(self):
        """IncidentType should include SSL_EXPIRING."""
        assert IncidentType.SSL_EXPIRING == "ssl_expiring"

    def test_all_expected_values(self):
        """IncidentType should contain all expected incident types."""
        expected = {
            "server_unreachable",
            "provisioning_failed",
            "service_down",
            "resource_exhausted",
            "ssl_expiring",
        }
        actual = {member.value for member in IncidentType}
        assert actual == expected
