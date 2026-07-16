"""Versioned schema and deterministic merge for environment contract fragments.

The contract is intentionally separate from the live deploy resolver until the
typed deploy migration consumes it.  This keeps schema validation available to
template and CI callers without changing current deploy behaviour.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_CONTRACT_VERSION = "1"

EnvSource = Literal[
    "user_secret",
    "generated_secret",
    "allocation",
    "derived",
    "literal",
]
EnvLiteralValue = str | int | float | bool


class EnvContractError(ValueError):
    """Raised when an environment contract fragment is invalid."""


class EnvContractMergeError(EnvContractError):
    """Raised when fragments cannot form one canonical contract."""


class EnvContractEntryBase(BaseModel):
    """Fields carried by every declared environment variable."""

    model_config = ConfigDict(extra="forbid")

    source: EnvSource
    environments: list[str] = Field(min_length=1)
    consumers: list[str] = Field(default_factory=list)
    required: bool
    description: str | None = None
    sensitive: bool = False


class UserSecretEntry(EnvContractEntryBase):
    """A secret supplied by a user and persisted at project scope."""

    source: Literal["user_secret"]
    consumers: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)
    sensitive: Literal[True] = True


class GeneratedSecretEntry(EnvContractEntryBase):
    """A secret generated and persisted by the platform."""

    source: Literal["generated_secret"]
    sensitive: Literal[True] = True


class AllocationEntry(EnvContractEntryBase):
    """A value resolved from a named allocation service or resource."""

    source: Literal["allocation"]
    service: str | None = None
    resource: str | None = None

    @model_validator(mode="after")
    def require_selector(self) -> AllocationEntry:
        if not self.service and not self.resource:
            raise ValueError("allocation requires a service or resource selector")
        return self


class DerivedEntry(EnvContractEntryBase):
    """A non-secret value derived from trusted deployment context."""

    source: Literal["derived"]
    sensitive: Literal[False] = False


class LiteralEntry(EnvContractEntryBase):
    """A committed non-secret value, typically for local or test environments."""

    source: Literal["literal"]
    value: EnvLiteralValue
    sensitive: Literal[False] = False


EnvContractEntry = Annotated[
    UserSecretEntry | GeneratedSecretEntry | AllocationEntry | DerivedEntry | LiteralEntry,
    Field(discriminator="source"),
]


class EnvContractFragment(BaseModel):
    """One owner-maintained piece of an environment contract."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[ENV_CONTRACT_VERSION] = ENV_CONTRACT_VERSION
    owner: str = Field(min_length=1)
    entries: dict[str, EnvContractEntry] = Field(default_factory=dict)


class CanonicalEnvContract(BaseModel):
    """Merged environment contract with a stable JSON representation."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[ENV_CONTRACT_VERSION] = ENV_CONTRACT_VERSION
    entries: dict[str, EnvContractEntry]

    def to_json_bytes(self) -> bytes:
        """Serialize the artifact with stable key ordering and compact JSON."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


def validate_env_contract_fragment(fragment: EnvContractFragment | dict) -> EnvContractFragment:
    """Validate one owner fragment at the schema boundary."""
    if isinstance(fragment, EnvContractFragment):
        return fragment
    return EnvContractFragment.model_validate(fragment)


def merge_env_contract_fragments(
    fragments: list[EnvContractFragment | dict],
) -> CanonicalEnvContract:
    """Validate and merge owner fragments into a deterministic artifact.

    Repeated keys are allowed only when their fully validated declarations are
    identical.  The owner name does not enter the resulting artifact, so moving
    an unchanged declaration between fragments cannot alter deploy semantics.
    """
    merged: dict[str, EnvContractEntry] = {}
    validated_fragments = (validate_env_contract_fragment(item) for item in fragments)
    for fragment in sorted(validated_fragments, key=lambda item: item.owner):
        for key, entry in fragment.entries.items():
            current = merged.get(key)
            if current is not None and current != entry:
                raise EnvContractMergeError(
                    f"incompatible environment contract declarations for {key}"
                )
            merged[key] = entry

    return CanonicalEnvContract(entries=dict(sorted(merged.items())))
