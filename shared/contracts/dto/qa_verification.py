"""What a QA run could not check, and where that is kept.

A check QA has no tool for — cause `qa_capability`
(`shared.contracts.qa_capabilities`) — is not a failure and not a pass. The QA
runner records it as **unverified** (`QAUnverifiedCheck`) and decides the
verdict from the checks it did run. The settling owner event carries the
unverified checks next to the names of the checks that passed
(`QAVerificationFacts`), and each unverified check of a settled run is written on
its project as a **verification gap** (`QAVerificationGap`).

Deliberately free of other contract imports: the owner-notification record and
the `po:input` event carry these facts, and neither may pull the run-result
module (and the queue contracts it imports) in with them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import BaseDTO


class QAUnverifiedOrigin(StrEnum):
    """Where in the QA runner an unverified check came from."""

    #: The executor reported the check failed with cause `qa_capability`.
    EXECUTOR = "executor"
    #: The executor reported the check not applicable, and this run recorded no
    #: transport refusal that grounds it.
    NOT_APPLICABLE = "not_applicable"
    #: An acceptance-criterion line QA has no tool for, withheld from the
    #: executor before it ran (`agents/qa/acceptance.py`).
    WITHHELD = "withheld"
    #: A kit package row the runner itself owed and had nothing to exercise it
    #: with: no jobs capability on the deployment, or no criterion naming the
    #: behaviour to fire.
    PACKAGE = "package"


class QAUnverifiedCheck(BaseModel):
    """One check QA could not run: what it was, why not, and where it arose."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    origin: QAUnverifiedOrigin


class QAVerificationFacts(BaseModel):
    """What the QA run that settles a story checked, and what it could not.

    Carried by the owner event that settles the story so PO can tell the user
    which of their requirements were checked and which were not. An empty
    ``unverified_checks`` is a run that checked everything it was given.
    """

    model_config = ConfigDict(extra="forbid")

    qa_run_id: str = Field(min_length=1)
    passed_checks: list[str] = Field(default_factory=list)
    unverified_checks: list[QAUnverifiedCheck] = Field(default_factory=list)


class QAVerificationGap(BaseDTO):
    """One unverified check of a settled QA run, as its project keeps it."""

    project_id: uuid.UUID
    story_id: str | None
    run_id: str
    name: str
    reason: str
    origin: QAUnverifiedOrigin
    #: When the settled run's gap was written on the project.
    created_at: datetime


class QAVerificationGapsFromRun(BaseModel):
    """Write the unverified checks of one settled QA Run on its project."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)


class QAVerificationGapsRecorded(BaseModel):
    """What one write added, by check name; a check already recorded is not repeated."""

    model_config = ConfigDict(extra="forbid")

    recorded: list[str] = Field(default_factory=list)
    already_recorded: int = 0
