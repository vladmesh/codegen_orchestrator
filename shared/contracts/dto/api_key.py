from shared.contracts.dto.base import TimestampedDTO


class APIKeyDTO(TimestampedDTO):
    """API Key response."""

    id: int
    service: str
    key_enc: str
