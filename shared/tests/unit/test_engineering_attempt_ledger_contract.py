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


def test_claude_evidence_is_the_only_source_of_claude_cost_and_usage() -> None:
    attempt = EngineeringAttemptLedgerInput(
        claude_evidence={
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "cache_read_tokens": 4,
            "cache_write_tokens": 5,
            "cost_microusd": 40_001,
        }
    )

    assert attempt.provider == "anthropic"
    assert attempt.cost_source is CostSource.PROVIDER_REPORTED
    assert attempt.cost_microusd == 40_001
    assert attempt.cache_read_tokens == 4
    assert attempt.cache_write_tokens == 5


def test_serialized_claude_evidence_can_be_revalidated_with_null_flat_placeholders() -> None:
    """A typed ledger payload remains valid after its normal wire serialization."""
    attempt = EngineeringAttemptLedgerInput(
        claude_evidence={"input_tokens": 12, "output_tokens": 3, "cost_microusd": 40_001}
    )

    wire = attempt.model_dump(mode="json")
    assert wire["provider"] == "anthropic"
    assert EngineeringAttemptLedgerInput.model_validate(wire).cost_microusd == 40_001


@pytest.mark.parametrize(
    "payload",
    [
        {
            "claude_evidence": {
                "provider": "anthropic",
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 14,
                "cost_microusd": 40_001,
            }
        },
        {
            "claude_evidence": {
                "provider": "anthropic",
                "input_tokens": 12,
                "output_tokens": 3,
                "total_tokens": 15,
                "cost_microusd": 40_001,
            },
            "input_tokens": 13,
        },
    ],
)
def test_claude_evidence_rejects_contradictory_or_mixed_records(payload: dict) -> None:
    """A ledger row cannot be assembled from separate Claude JSON records."""
    with pytest.raises(ValidationError):
        EngineeringAttemptLedgerInput(**payload)
