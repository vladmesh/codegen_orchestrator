"""A project's QA probe library: probes that passed QA runs executed, kept for later runs.

The library is filled only from a passed QA Run's own `probe_runs` — records the
capability endpoint already scrubbed and bounded — and it is offered back to
later QA runs of the same project as files in the executor's workspace, beside
the platform-shipped seed probes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import BaseDTO
from shared.contracts.dto.run_result import QAProbeFileKind, QAProbePlatform

#: At most this many entries are kept per project; storing past it evicts the
#: entries with the oldest `updated_at`. It is not below the runner's per-run
#: probe cap (`MAX_PROBES`), so one passed run never evicts its own entries.
QA_PROBE_LIBRARY_CAP = 50

#: The only names a library entry can have. The store from a passed run keeps a
#: probe whose name matches and skips any other, never rewriting it, so the
#: name is also the entry's file stem: unique within its platform directory by
#: the table's (project, platform, name) key, free of path separators, quoting
#: and escapes, and bounded in length.
QA_PROBE_LIBRARY_NAME_MAX = 64
QA_PROBE_LIBRARY_NAME_PATTERN = rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{QA_PROBE_LIBRARY_NAME_MAX - 1}}}$"
_NAME = QA_PROBE_LIBRARY_NAME_PATTERN.removeprefix("^").removesuffix("$")

#: A library file the runner hands a QA executor: the index, or one probe named
#: `<platform>/<entry name>.<py|sh>`, so no file can land outside the library.
QA_PROBE_LIBRARY_FILE_PATTERN = rf"^(?:index\.json|(?:telegram|http|web)/{_NAME}\.(?:py|sh))$"
QA_PROBE_LIBRARY_INDEX = "index.json"
#: A probe source is bounded by the capability endpoint at 20,000 characters.
QA_PROBE_LIBRARY_FILE_MAX = 64_000

#: Platform-shipped seeds a run can be offered at most, and the longest
#: argument synopsis one declares for its usage line.
QA_PROBE_LIBRARY_SEEDS_MAX = 13
QA_PROBE_LIBRARY_USAGE_ARGS_MAX = 64
#: An entry's origin is the storing Run's id, at most the `runs.id` width.
QA_PROBE_LIBRARY_ORIGIN_MAX = 255
#: One index row without its name, origin and usage arguments: the keys, the
#: platform (thrice), the library path (twice) and the separators.
_INDEX_ROW_OVERHEAD = 256
#: The index's worst case, by arithmetic rather than by trust in stored names:
#: every offered row names its entry four times (name, file, and the file and
#: name again in its usage line) and a JSON escape is at most six characters,
#: which only the origin can need; a canonical name needs none.
QA_PROBE_LIBRARY_INDEX_MAX = 64 + (QA_PROBE_LIBRARY_CAP + QA_PROBE_LIBRARY_SEEDS_MAX) * (
    _INDEX_ROW_OVERHEAD
    + 4 * QA_PROBE_LIBRARY_NAME_MAX
    + 6 * QA_PROBE_LIBRARY_ORIGIN_MAX
    + QA_PROBE_LIBRARY_USAGE_ARGS_MAX
)
#: The project cap, the seeds and the index.
QA_PROBE_LIBRARY_MAX_FILES = QA_PROBE_LIBRARY_CAP + QA_PROBE_LIBRARY_SEEDS_MAX + 1


def is_probe_library_name(name: str) -> bool:
    """Whether `name` can enter a project's library as it is."""
    return re.fullmatch(QA_PROBE_LIBRARY_NAME_PATTERN, name) is not None


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
    """What one store changed: entries written and entries evicted, as `platform/name`.

    `skipped` counts the run's otherwise eligible probes whose name is not a
    library name (`QA_PROBE_LIBRARY_NAME_PATTERN`); they are not stored.
    """

    model_config = ConfigDict(extra="forbid")

    stored: list[str] = Field(default_factory=list)
    evicted: list[str] = Field(default_factory=list)
    skipped: int = 0


class QAProbeLibraryFile(BaseModel):
    """One file written under the QA executor's library directory."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=QA_PROBE_LIBRARY_FILE_PATTERN)
    content: str = Field(max_length=QA_PROBE_LIBRARY_INDEX_MAX)

    @model_validator(mode="after")
    def _a_probe_file_is_a_bounded_source(self) -> QAProbeLibraryFile:
        if self.path != QA_PROBE_LIBRARY_INDEX and len(self.content) > QA_PROBE_LIBRARY_FILE_MAX:
            raise ValueError(
                f"a probe library file is at most {QA_PROBE_LIBRARY_FILE_MAX} characters"
            )
        return self


def check_probe_library_files(files: Sequence[QAProbeLibraryFile]) -> None:
    """Refuse a file set no executor workspace can be given: too many or a repeated path."""
    if len(files) > QA_PROBE_LIBRARY_MAX_FILES:
        raise ValueError(f"a probe library is at most {QA_PROBE_LIBRARY_MAX_FILES} files")
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)):
        raise ValueError("probe library paths must be unique")
