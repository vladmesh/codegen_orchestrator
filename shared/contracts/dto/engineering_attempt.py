"""Canonical facts recorded for one terminal engineering coding attempt."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class CostSource(StrEnum):
    """Where an attempt's exact cost came from."""

    PROVIDER_REPORTED = "provider_reported"
    UNKNOWN = "unknown"


class ClaudeResultEvidence(BaseModel):
    """Facts derived together from one Claude CLI final-result object.

    ``cost_microusd`` is converted from Claude's ``total_cost_usd`` before this
    object crosses a queue or API boundary. A missing value remains unknown;
    it is never substituted with a tariff or zero.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic"] = "anthropic"
    model: str | None = Field(default=None, max_length=255)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_total_tokens(self) -> "ClaudeResultEvidence":
        if self.input_tokens is not None and self.output_tokens is not None:
            computed_total = self.input_tokens + self.output_tokens
            if self.total_tokens is None:
                self.total_tokens = computed_total
            elif self.total_tokens != computed_total:
                raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


_CLAUDE_FLAT_FACT_FIELDS = frozenset(
    {
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_microusd",
        "cost_source",
    }
)


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
    claude_evidence: ClaudeResultEvidence | None = None

    @model_validator(mode="before")
    @classmethod
    def _prevent_mixed_claude_facts(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("claude_evidence") is not None:
            try:
                evidence = ClaudeResultEvidence.model_validate(value["claude_evidence"])
            except ValidationError:
                # Let the field validator report malformed evidence precisely.
                return value
            expected = {
                "provider": evidence.provider,
                "model": evidence.model,
                "input_tokens": evidence.input_tokens,
                "output_tokens": evidence.output_tokens,
                "total_tokens": evidence.total_tokens,
                "cache_read_tokens": evidence.cache_read_tokens,
                "cache_write_tokens": evidence.cache_write_tokens,
                "cost_microusd": evidence.cost_microusd,
                "cost_source": (
                    CostSource.PROVIDER_REPORTED
                    if evidence.cost_microusd is not None
                    else CostSource.UNKNOWN
                ),
            }
            mixed = {
                field
                for field in _CLAUDE_FLAT_FACT_FIELDS
                if value.get(field) is not None and value[field] != expected[field]
            }
            if mixed:
                raise ValueError("claude_evidence cannot be combined with flat provider facts")
        return value

    @model_validator(mode="after")
    def _validate_provenance(self) -> "EngineeringAttemptLedgerInput":
        if self.claude_evidence is not None:
            evidence = self.claude_evidence
            self.provider = evidence.provider
            self.model = evidence.model
            self.input_tokens = evidence.input_tokens
            self.output_tokens = evidence.output_tokens
            self.total_tokens = evidence.total_tokens
            self.cache_read_tokens = evidence.cache_read_tokens
            self.cache_write_tokens = evidence.cache_write_tokens
            self.cost_microusd = evidence.cost_microusd
            self.cost_source = (
                CostSource.PROVIDER_REPORTED
                if evidence.cost_microusd is not None
                else CostSource.UNKNOWN
            )
        known_usage = tuple(
            usage for usage in (self.input_tokens, self.output_tokens) if usage is not None
        )
        if self.total_tokens is not None and any(
            self.total_tokens < usage for usage in known_usage
        ):
            raise ValueError("total_tokens cannot be less than a known usage component")
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
