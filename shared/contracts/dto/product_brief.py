"""Durable, confirmed product intent and architect requirement disposition."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
import re
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SECRET_NAME = re.compile(r"(?:secret|token|password|api[_-]?key|private[_-]?key|credential)", re.I)


class InitialSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    value: Any
    scope: str = Field(min_length=1, max_length=64)
    subject: str | None = Field(default=None, max_length=255)

    @field_validator("key", "scope", "subject")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("setting text must not be blank")
        return value

    @model_validator(mode="after")
    def _safe_json_setting(self) -> InitialSetting:
        if _SECRET_NAME.search(self.key) or (self.subject and _SECRET_NAME.search(self.subject)):
            raise ValueError("secret-bearing settings are not product settings")
        try:
            encoded = json.dumps(self.value)
        except (TypeError, ValueError) as exc:
            raise ValueError("setting value must be JSON") from exc
        if _SECRET_NAME.search(encoded):
            raise ValueError("secret-bearing settings are not product settings")
        return self


class MustRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10000)
    source: str = Field(min_length=1, max_length=10000)

    @field_validator("id", "text", "source")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("requirement fields must not be blank")
        return value


class ProductBriefContent(BaseModel):
    """The versioned user-facing content that is frozen at confirmation."""

    model_config = ConfigDict(extra="forbid")

    intended_users: list[str] = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    must_requirements: list[MustRequirement] = Field(min_length=1)
    initial_settings: list[InitialSetting] = Field(default_factory=list)

    @field_validator("intended_users", "languages")
    @classmethod
    def _non_blank_values(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("brief values must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _requirement_ids_are_unique(self) -> ProductBriefContent:
        ids = [requirement.id for requirement in self.must_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("must requirement IDs must be unique")
        return self


class ProductBriefCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    title: str = Field(min_length=1, max_length=500)
    content: ProductBriefContent
    request_id: str = Field(min_length=1, max_length=255)


class ProductBriefConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=255)
    content: ProductBriefContent


class ProductBriefRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: uuid.UUID
    story_id: str | None
    revision: int
    title: str
    content: ProductBriefContent
    confirmed_at: datetime | None
    confirmation_request_id: str | None
    coverage_admitted_at: datetime | None


class CoverageDisposition(StrEnum):
    RETURNED = "returned"


class RequirementCoverageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, max_length=255)
    repository_acceptance_contract: str | None = Field(default=None, max_length=10000)
    returned_reason: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def _has_one_disposition(self) -> RequirementCoverageCreate:
        coverage = self.task_id or self.repository_acceptance_contract
        if not coverage and not self.returned_reason:
            raise ValueError("coverage needs a task, acceptance contract, or returned reason")
        if coverage and self.returned_reason:
            raise ValueError("coverage and returned reason are mutually exclusive")
        return self


class RequirementCoverageRead(RequirementCoverageCreate):
    id: int
    brief_id: str


class ProductBriefAdmissionOutcome(StrEnum):
    ADMITTED = "admitted"
    ALREADY_ADMITTED = "already_admitted"
    INCOMPLETE = "incomplete"


class ProductBriefAdmissionRead(BaseModel):
    """Durable result of releasing one brief-backed Story's planned tasks."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str
    story_id: str
    outcome: ProductBriefAdmissionOutcome
    missing_requirement_ids: list[str] = Field(default_factory=list)
    released_task_ids: list[str] = Field(default_factory=list)
