"""Promo-code invariants that do not need a database."""

from pydantic import ValidationError
import pytest

from src.schemas.promo_code import PromoCodeBatchCreate


def test_batch_rejects_zero_attempt_reservation() -> None:
    with pytest.raises(ValidationError):
        PromoCodeBatchCreate(
            quantity=1,
            credits_microusd=100,
            attempt_reservation_microusd=0,
        )


def test_batch_rejects_negative_credits() -> None:
    with pytest.raises(ValidationError):
        PromoCodeBatchCreate(
            quantity=1,
            credits_microusd=-1,
            attempt_reservation_microusd=1,
        )
