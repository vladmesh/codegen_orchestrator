"""A project's QA probe library: probes that passed QA runs executed, kept for later runs.

The library is filled only from a passed QA Run's own `probe_runs` — records the
capability endpoint already scrubbed and bounded — and it is offered back to
later QA runs of the same project as files in the executor's workspace, beside
the platform-shipped seed probes.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import BaseDTO
from shared.contracts.dto.run_result import QAProbeFileKind, QAProbePlatform

#: At most this many entries are kept per project; storing past it evicts the
#: entries with the oldest `updated_at`. It is not below the runner's per-run
#: probe cap (`MAX_PROBES`), so one passed run never evicts its own entries.
QA_PROBE_LIBRARY_CAP = 50

#: A library file the runner hands a QA executor: the index, or one probe under
#: its platform directory. The file stem is a filesystem-safe form of the entry
#: name, so no entry can name a path outside the library directory.
QA_PROBE_LIBRARY_FILE_PATTERN = (
    r"^(?:index\.json|(?:telegram|http|web)/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.(?:py|sh))$"
)
QA_PROBE_LIBRARY_INDEX = "index.json"
#: A probe source is bounded by the capability endpoint at 20,000 characters;
#: the index of a full library stays well under this too.
QA_PROBE_LIBRARY_FILE_MAX = 64_000
#: The project cap, the seeds and the index.
QA_PROBE_LIBRARY_MAX_FILES = QA_PROBE_LIBRARY_CAP + 14


class QAProbeLibraryEntry(BaseDTO):
    """One stored probe of a project's library, as the API returns it."""

    project_id: uuid.UUID
    platform: QAProbePlatform
    name: str
    source: str
    file_kind: QAProbeFileKind
    origin_run_id: str
    created_at: datetime
    updated_at: datetime


class QAProbeLibraryStoreFromRun(BaseModel):
    """Store the eligible probes of one passed QA Run in its project's library."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)


class QAProbeLibraryStored(BaseModel):
    """What one store changed: entries written and entries evicted, as `platform/name`."""

    model_config = ConfigDict(extra="forbid")

    stored: list[str] = Field(default_factory=list)
    evicted: list[str] = Field(default_factory=list)


class QAProbeLibraryFile(BaseModel):
    """One file written under the QA executor's library directory."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=QA_PROBE_LIBRARY_FILE_PATTERN)
    content: str = Field(max_length=QA_PROBE_LIBRARY_FILE_MAX)
