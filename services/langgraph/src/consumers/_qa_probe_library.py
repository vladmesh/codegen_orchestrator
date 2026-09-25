"""The probe library a QA run offers its executor: platform seeds and its project's probes.

Prepared once, at run start, on the management host. The files travel on the
executor's create request and worker-manager writes them under
`QA_PROBE_LIBRARY_PATH` before the executor gets its turn; `index.json` there
lists every offered probe with its origin and a one-line usage. What was offered,
and why the project's own entries were not, is kept on the Run as
`QARunResult.probe_library`.

A stored library can never make a later run fail. Names are canonical when the
API stores them (`QA_PROBE_LIBRARY_NAME_PATTERN`), so an entry's name is its
file stem; and whatever the read returned, the build is total: stored entries
that cannot be laid out as one executor's library are dropped whole, the run
gets the seeds, and the offer says why in `build_failure`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import json
import shlex

import structlog

from shared.contracts.dto.qa_probe_library import (
    QA_PROBE_LIBRARY_INDEX,
    QAProbeLibraryEntry,
    QAProbeLibraryFile,
    check_probe_library_files,
)
from shared.contracts.dto.run_result import QAProbeLibraryOffer, QAProbeLibraryOffered
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH
from shared.qa_probe_library import SEED_ORIGIN, QAProbeSeed, seed_probes

logger = structlog.get_logger(__name__)

#: A note on the Run says what went wrong, not everything the error carried.
_NOTE_MAX = 500


@dataclass(frozen=True)
class QAProbeLibrary:
    """The files an executor is given and the Run's record of the offer."""

    files: list[QAProbeLibraryFile]
    offer: QAProbeLibraryOffer


def build_probe_library(
    *,
    project_id: str,
    seeds: Sequence[QAProbeSeed],
    entries: Sequence[QAProbeLibraryEntry],
    read_failure: str | None,
    build_failure: str | None = None,
) -> QAProbeLibrary:
    """Lay out the seeds, then this project's entries, and index them.

    A seed shadows a project entry of the same platform and name, and an entry
    of another project is never offered, whatever the read returned. An entry
    is written as `<platform>/<name>.<kind>`; a name that is not a library
    name, a name read twice (its path repeats), too many files or an index
    over its budget raises, as the executor's create request would.
    """
    files: list[QAProbeLibraryFile] = []
    index: list[dict] = []
    offered: list[QAProbeLibraryOffered] = []
    seeded = {(seed.platform.value, seed.name) for seed in seeds}

    def offer(
        *, platform: str, name: str, file_kind: str, source: str, origin: str, usage_args: str
    ):
        path = f"{platform}/{name}.{file_kind}"
        location = f"{QA_PROBE_LIBRARY_PATH}/{path}"
        files.append(QAProbeLibraryFile(path=path, content=source))
        index.append(
            {
                "name": name,
                "platform": platform,
                "origin": origin,
                "file": location,
                "usage": f"qa probe {platform} {shlex.quote(name)} {location} {usage_args}",
            }
        )
        offered.append(QAProbeLibraryOffered(platform=platform, name=name, origin=origin))

    for seed in seeds:
        offer(
            platform=seed.platform.value,
            name=seed.name,
            file_kind=seed.file_kind.value,
            source=seed.source(),
            origin=SEED_ORIGIN,
            usage_args=seed.arguments,
        )
    for entry in entries:
        key = (entry.platform.value, entry.name)
        if str(entry.project_id) != project_id:
            logger.warning(
                "qa_probe_library_foreign_entry_dropped",
                project_id=project_id,
                entry_project_id=str(entry.project_id),
                name=entry.name,
            )
            continue
        if key in seeded:
            continue
        offer(
            platform=entry.platform.value,
            name=entry.name,
            file_kind=entry.file_kind.value,
            source=entry.source,
            origin=entry.origin_run_id,
            usage_args="[ARG ...]",
        )
    files.append(
        QAProbeLibraryFile(
            path=QA_PROBE_LIBRARY_INDEX,
            content=json.dumps({"probes": index}, ensure_ascii=False, indent=1) + "\n",
        )
    )
    check_probe_library_files(files)
    return QAProbeLibrary(
        files=files,
        offer=QAProbeLibraryOffer(
            offered=offered, read_failure=read_failure, build_failure=build_failure
        ),
    )


def _note(what: str, exc: Exception) -> str:
    return f"{what}: {type(exc).__name__}: {exc}"[:_NOTE_MAX]


async def prepare_probe_library(
    *,
    project_id: str,
    telegram_bot: bool,
    read_entries: Callable[[str], Awaitable[list[QAProbeLibraryEntry]]],
) -> QAProbeLibrary:
    """Read this project's library and lay it out with the seeds this run is due.

    `telegram_bot` is whether the run tests a Telegram bot — the runner's
    `bot_username`. A failed read is not a failed run: the executor still gets
    the seeds, and the Run records why it got nothing more.
    """
    seeds = seed_probes(telegram_bot=telegram_bot)
    read_failure = None
    try:
        entries = await read_entries(project_id)
    except Exception as exc:
        read_failure = _note("the project's probe library could not be read", exc)
        logger.warning("qa_probe_library_read_failed", project_id=project_id, error=str(exc))
        entries = []
    try:
        library = build_probe_library(
            project_id=project_id, seeds=seeds, entries=entries, read_failure=read_failure
        )
    except Exception as exc:
        # Rows written before names were canonical, or by any other path, end
        # here rather than in the executor's create request. The seeds alone
        # always build.
        build_failure = _note("the project's probe library could not be built", exc)
        logger.warning(
            "qa_probe_library_build_failed",
            project_id=project_id,
            entries=len(entries),
            error=build_failure,
        )
        library = build_probe_library(
            project_id=project_id,
            seeds=seeds,
            entries=[],
            read_failure=read_failure,
            build_failure=build_failure,
        )
    logger.info(
        "qa_probe_library_prepared",
        project_id=project_id,
        offered=len(library.offer.offered),
        read_failure=read_failure,
        build_failure=library.offer.build_failure,
    )
    return library
