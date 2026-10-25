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


# Telegram refuses a sendMessage text longer than this.
ADMIN_TEXT_LIMIT = 4096
CANDIDATE_BLOCK_LIMIT = 1500
# A stored story id is at most 255 characters. Reserve room for its carried-text marker.
CANDIDATE_TEXT_LIMIT = ADMIN_TEXT_LIMIT - 512
RATIONALE_CUT = " [rationale truncated]"


def _candidate_block(
    project: Project, story: Story, candidate: PackageCandidate, limit: int
) -> str:
    """One candidate's notice: identity facts and the warning whole, the rationale cut to fit."""
    path = f"/package-candidates/{candidate.id}"

    def block(rationale: str) -> str:
        return (
            f"Package candidate from product {project.title} (project {project.id}), "
            f"story {story.id}: {candidate.package_name} {candidate.package_version} at "
            f"{candidate.path}, commit {candidate.commit} in repository {candidate.repository_id}. "
            f"Capability: {candidate.capability}. Rationale: {rationale}. "
            "The code is unreviewed and exclusive to this product; it awaits the owner's "
            "personal code and security review before any release. "
            f"Full record in the admin console at {path}."
        )

    text = block(candidate.rationale)
    if len(text) <= limit:
        return text
    room = limit - len(block("")) - len(RATIONALE_CUT)
    return block(candidate.rationale[: max(room, 0)] + RATIONALE_CUT)


async def package_candidate_admin_text(
    story: Story, candidates: list[PackageCandidate], db: AsyncSession
) -> str:
    """The administrators' notice of the candidates one completion recorded."""
    project = await db.get(Project, story.project_id)
    separators = 2 * (len(candidates) - 1)
    block_limit = min(CANDIDATE_BLOCK_LIMIT, (CANDIDATE_TEXT_LIMIT - separators) // len(candidates))
    return "\n\n".join(
        _candidate_block(project, story, candidate, block_limit) for candidate in candidates
    )


def bounded_admin_text(story_id: str, carried: str | None, candidate_text: str) -> str:
    """Carried administrator text before the candidate notice, within one Telegram message.

    The candidate notice stays whole; only the carried text is shortened, with a marker.
    """
    if len(candidate_text) > CANDIDATE_TEXT_LIMIT:
        raise ValueError("package candidate text exceeds the one-message reserve")
    if not carried:
        return candidate_text
    combined = f"{carried}\n\n{candidate_text}"
    if len(combined) <= ADMIN_TEXT_LIMIT:
        return combined
    marker = f" [earlier administrator text for story {story_id} truncated]"
    room = ADMIN_TEXT_LIMIT - len(candidate_text) - len(marker) - 2
    return f"{carried[: max(room, 0)]}{marker}\n\n{candidate_text}"
