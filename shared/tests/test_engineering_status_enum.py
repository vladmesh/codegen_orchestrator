"""Unit tests for EngineeringStatus StrEnum."""

from shared.contracts.dto.engineering import EngineeringStatus


class TestEngineeringStatusEnum:
    """EngineeringStatus values and StrEnum semantics."""

    def test_can_be_used_in_dict_key(self):
        """Enum members work as dict keys interchangeable with strings."""
        d = {EngineeringStatus.DONE: "success"}
        assert d["done"] == "success"

    def test_constructable_from_string(self):
        """Can construct enum from string value."""
        assert EngineeringStatus("idle") is EngineeringStatus.IDLE
        assert EngineeringStatus("gave_up") is EngineeringStatus.GAVE_UP
