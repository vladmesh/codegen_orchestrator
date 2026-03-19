"""Unit tests for EngineeringStatus StrEnum."""

from shared.contracts.dto.engineering import EngineeringStatus


class TestEngineeringStatusEnum:
    """EngineeringStatus values and StrEnum semantics."""

    def test_has_exactly_four_members(self):
        assert len(EngineeringStatus) == 4

    def test_idle_value(self):
        assert EngineeringStatus.IDLE == "idle"

    def test_done_value(self):
        assert EngineeringStatus.DONE == "done"

    def test_gave_up_value(self):
        assert EngineeringStatus.GAVE_UP == "gave_up"

    def test_failed_value(self):
        assert EngineeringStatus.FAILED == "failed"

    def test_strenum_equality_with_bare_strings(self):
        """StrEnum members must compare equal to their string values."""
        assert EngineeringStatus.IDLE == "idle"
        assert EngineeringStatus.DONE == "done"
        assert EngineeringStatus.GAVE_UP == "gave_up"
        assert EngineeringStatus.FAILED == "failed"

    def test_can_be_used_in_dict_key(self):
        """Enum members work as dict keys interchangeable with strings."""
        d = {EngineeringStatus.DONE: "success"}
        assert d["done"] == "success"

    def test_constructable_from_string(self):
        """Can construct enum from string value."""
        assert EngineeringStatus("idle") is EngineeringStatus.IDLE
        assert EngineeringStatus("gave_up") is EngineeringStatus.GAVE_UP
