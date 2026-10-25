"""The package candidates a completing story's DONE product-local INSTALLs record.

Called inside the story completion transaction; it never commits.
"""

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.local_install import (
    LocalPackageInstall,
    local_install_task_id,
    parse_install_target,
)
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.models import PackageCandidate, Project, Story, Task
from shared.notifications import ADMIN_MESSAGE_BODY_LIMIT
from shared.telegram_text import utf16_length


def _unrecordable(install: Task, cause: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "package_candidate_unrecordable", "task_id": install.id, "cause": cause},
    )


async def _story_builds(
    story: Story, db: AsyncSession
) -> list[tuple[Task, Task, LocalPackageInstall]]:
    """Each DONE `package_source` INSTALL of the story with its source FEATURE, oldest first."""
    rows = await db.scalars(
        select(Task)
        .where(
            Task.story_id == story.id,
            Task.type == TaskType.INSTALL.value,
            Task.status == TaskStatus.DONE.value,
        )
        .order_by(Task.created_at, Task.id)
    )
    installs = []
    for install in rows.all():
        try:
            target = parse_install_target(install.install)
        except ValidationError as exc:
            raise _unrecordable(install, "install_target_invalid") from exc
        if isinstance(target, LocalPackageInstall):
            installs.append((install, target))
    if not installs:
        return []
    sources = await db.scalars(
        select(Task).where(Task.story_id == story.id, Task.package_build.is_not(None))
    )
    by_install = {local_install_task_id(source.id): source for source in sources.all()}
    builds = []
    for install, target in installs:
        source = by_install.get(install.id)
        if source is None:
            raise _unrecordable(install, "source_feature_missing")
        if source.description is None:
            raise _unrecordable(install, "source_feature_description_missing")
        builds.append((source, install, target))
    return builds


async def record_package_candidates(story: Story, db: AsyncSession) -> list[PackageCandidate]:
    """Upsert one candidate per product package from the story's DONE local INSTALLs.

    A rebuilt package keeps its row at the newest commit; an INSTALL whose target does not
    parse, or whose source FEATURE is unknown or undescribed, refuses the completion (409).
    """
    recorded: dict[int, PackageCandidate] = {}
    for source, install, target in await _story_builds(story, db):
        values = {
            "story_id": story.id,
            "repository_id": target.repository_id,
            "source_task_id": source.id,
            "install_task_id": install.id,
            "path": target.path,
            "package_distribution": target.package.distribution,
            "package_version": target.package.version,
            "commit": target.commit,
            "core_version": target.core_version,
            "python_version": target.python_version,
            "tooling_commit": target.tooling_commit,
            "capability": source.title,
            "rationale": source.description,
        }
        candidate = await db.scalar(
            insert(PackageCandidate)
            .values(project_id=target.project_id, package_name=target.package.name, **values)
            .on_conflict_do_update(
                constraint="uq_package_candidates_project_package",
                set_={**values, "updated_at": func.now()},
            )
            .returning(PackageCandidate)
            .execution_options(populate_existing=True)
        )
        recorded[candidate.id] = candidate
    return list(recorded.values())


# A story id is up to 255 characters, each up to two UTF-16 units; this keeps room for the
# carried-text marker that names it.
CANDIDATE_TEXT_LIMIT = ADMIN_MESSAGE_BODY_LIMIT - 1024
TITLE_LIMIT = 100
TITLE_CUT = " [title truncated]"


def _utf16_prefix(text: str, units: int) -> str:
    """The longest prefix of `text` at most `units` UTF-16 code units long."""
    used = 0
    for index, char in enumerate(text):
        used += utf16_length(char)
        if used > units:
            return text[:index]
    return text


def _capped_title(title: str) -> str:
    if utf16_length(title) <= TITLE_LIMIT:
        return title
    return _utf16_prefix(title, TITLE_LIMIT) + TITLE_CUT


def _more(count: int) -> str:
    return f"+{count} more: /package-candidates"


async def package_candidate_admin_text(
    story: Story, candidates: list[PackageCandidate], db: AsyncSession
) -> str:
    """The administrators' notice of the candidates one completion recorded.

    A header with the review warning, then one line per candidate in recording order while
    they fit `CANDIDATE_TEXT_LIMIT`, then a count of the rest; the admin pages hold the facts.
    """
    project = await db.get(Project, story.project_id)
    header = (
        f"Package candidates from product {_capped_title(project.title)} "
        f"(project {project.id}), story {story.id}: {len(candidates)} recorded. "
        "The code is unreviewed and exclusive to this product; it awaits the owner's "
        "personal code and security review before any release."
    )
    lines = [header]
    used = utf16_length(header)
    for listed, candidate in enumerate(candidates):
        line = (
            f"{candidate.package_name} {candidate.package_version}, "
            f"commit {candidate.commit[:12]}: /package-candidates/{candidate.id}"
        )
        rest = len(candidates) - listed - 1
        reserve = 1 + utf16_length(_more(rest)) if rest else 0
        if used + 1 + utf16_length(line) + reserve > CANDIDATE_TEXT_LIMIT:
            lines.append(_more(rest + 1))
            break
        lines.append(line)
        used += 1 + utf16_length(line)
    return "\n".join(lines)


def bounded_admin_text(story_id: str, carried: str | None, candidate_text: str) -> str:
    """Carried administrator text before the candidate notice, within `ADMIN_MESSAGE_BODY_LIMIT`.

    The candidate notice stays whole; only the carried text is shortened, with a marker.
    """
    if not carried:
        return candidate_text
    combined = f"{carried}\n\n{candidate_text}"
    if utf16_length(combined) <= ADMIN_MESSAGE_BODY_LIMIT:
        return combined
    marker = f" [earlier administrator text for story {story_id} truncated]"
    room = ADMIN_MESSAGE_BODY_LIMIT - utf16_length(f"{marker}\n\n{candidate_text}")
    return f"{_utf16_prefix(carried, room)}{marker}\n\n{candidate_text}"
