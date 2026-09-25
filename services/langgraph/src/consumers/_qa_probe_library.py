"""The probe library a QA run offers its executor: platform seeds and its project's probes.

Prepared once, at run start, on the management host. The files travel on the
executor's create request and worker-manager writes them under
`QA_PROBE_LIBRARY_PATH` before the executor gets its turn; `index.json` there
lists every offered probe with its origin and a one-line usage. What was offered,
and why the project's own entries were not, is kept on the Run as
`QARunResult.probe_library`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
import shlex

import structlog

from shared.contracts.dto.qa_probe_library import (
    QA_PROBE_LIBRARY_INDEX,
    QAProbeLibraryEntry,
    QAProbeLibraryFile,
)
from shared.contracts.dto.run_result import QAProbeLibraryOffer, QAProbeLibraryOffered
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH
from shared.qa_probe_library import SEED_ORIGIN, QAProbeSeed, seed_probes

logger = structlog.get_logger(__name__)

_STEM_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_STEM_MAX = 55


@dataclass(frozen=True)
class QAProbeLibrary:
    """The files an executor is given and the Run's record of the offer."""

    files: list[QAProbeLibraryFile]
    offer: QAProbeLibraryOffer


def probe_file_stem(name: str) -> str:
    """A file stem for an entry name that cannot leave its platform directory.

    A name that is already a safe stem is kept as it is; any other name keeps
    its safe characters and gains a short digest of the whole name, so two
    names never share a file.
    """
    stem = _STEM_UNSAFE.sub("_", name).lstrip("._-")[:_STEM_MAX]
    if stem and stem == name:
        return stem
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{stem or 'probe'}-{digest}"


def build_probe_library(
    *,
    project_id: str,
    seeds: Sequence[QAProbeSeed],
    entries: Sequence[QAProbeLibraryEntry],
    read_failure: str | None,
) -> QAProbeLibrary:
    """Lay out the seeds, then this project's entries, and index them.

    A seed shadows a project entry of the same platform and name, and an entry
    of another project is never offered, whatever the read returned.
    """
    files: list[QAProbeLibraryFile] = []
    index: list[dict] = []
    offered: list[QAProbeLibraryOffered] = []
    taken: set[tuple[str, str]] = set()

    def offer(
        *, platform: str, name: str, file_kind: str, source: str, origin: str, usage_args: str
    ):
        path = f"{platform}/{probe_file_stem(name)}.{file_kind}"
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
        taken.add((platform, name))

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
        if key in taken:
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
    return QAProbeLibrary(
        files=files,
        offer=QAProbeLibraryOffer(offered=offered, read_failure=read_failure),
    )


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
    read_failure = None
    try:
        entries = await read_entries(project_id)
    except Exception as exc:
        read_failure = f"the project's probe library could not be read: {type(exc).__name__}: {exc}"
        logger.warning("qa_probe_library_read_failed", project_id=project_id, error=str(exc))
        entries = []
    library = build_probe_library(
        project_id=project_id,
        seeds=seed_probes(telegram_bot=telegram_bot),
        entries=entries,
        read_failure=read_failure,
    )
    logger.info(
        "qa_probe_library_prepared",
        project_id=project_id,
        offered=len(library.offer.offered),
        read_failure=read_failure,
    )
    return library
