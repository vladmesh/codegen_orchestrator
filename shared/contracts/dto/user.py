from shared.contracts.dto.base import TimestampedDTO


class UserDTO(TimestampedDTO):
    """User response."""

    id: int
    telegram_id: int

    is_admin: bool = False
