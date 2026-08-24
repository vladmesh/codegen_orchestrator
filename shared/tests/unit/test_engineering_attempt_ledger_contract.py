"""The engineering-attempt ledger accepts only attributable, exact facts."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.engineering_attempt import (
    CostSource,
    EngineeringAttemptLedgerInput,
)


def test_unknown_cost_keeps_usage_without_inventing_zero_cost() -> None:
    attempt = EngineeringAttemptLedgerInput(
        provider="anthropic",
        model="claude-sonnet",
        input_tokens=12,
        output_tokens=3,
        cost_source=CostSource.UNKNOWN,
    )

    assert attempt.cost_microusd is None
    assert attempt.total_tokens == 15


@pytest.mark.parametrize(
    "payload",
    [
        {"input_tokens": 10, "total_tokens": 9},
        {"output_tokens": 10, "total_tokens": 9},
    ],
)
def test_total_tokens_cannot_be_less_than_known_partial_usage(payload: dict) -> None:
    """One available usage fact is enough to disprove a supplied total."""
    with pytest.raises(ValidationError, match="total_tokens"):
        EngineeringAttemptLedgerInput(**payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"cost_source": "unknown", "cost_microusd": 0},
        {"cost_source": "provider_reported", "cost_microusd": 12},
        {
            "provider": "anthropic",
            "cost_source": "provider_reported",
            "cost_microusd": None,
        },
    ],
)
def test_cost_provenance_cannot_be_inconsistent(payload: dict) -> None:
    with pytest.raises(ValidationError):
        EngineeringAttemptLedgerInput(**payload)
