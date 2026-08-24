"""Canonical facts recorded for one terminal engineering coding attempt."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CostSource(StrEnum):
    """Where an attempt's exact cost came from."""

    PROVIDER_REPORTED = "provider_reported"
    UNKNOWN = "unknown"


class EngineeringAttemptLedgerInput(BaseModel):
    """Provider facts accepted while finalizing an engineering run.

    Money is integer micro-USD: one unit is 0.000001 USD.  ``unknown`` means
    no monetary fact was supplied; it is never represented by a zero amount.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=255)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)
    cost_source: CostSource = CostSource.UNKNOWN

    @model_validator(mode="after")
    def _validate_provenance(self) -> "EngineeringAttemptLedgerInput":
        if self.input_tokens is not None and self.output_tokens is not None:
            computed_total = self.input_tokens + self.output_tokens
            if self.total_tokens is None:
                self.total_tokens = computed_total
            elif self.total_tokens != computed_total:
                raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.cost_source is CostSource.UNKNOWN:
            if self.cost_microusd is not None:
                raise ValueError("unknown cost_source must not carry cost_microusd")
        elif not self.provider or self.cost_microusd is None:
            raise ValueError("provider_reported cost requires provider and cost_microusd")
        return self
