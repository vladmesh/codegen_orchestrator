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


async def package_candidate_admin_text(
    story: Story, candidates: list[PackageCandidate], db: AsyncSession
) -> str:
    """The administrators' notice of the candidates one completion recorded."""
    project = await db.get(Project, story.project_id)
    return "\n\n".join(
        f"Package candidate from product {project.title} (project {project.id}), "
        f"story {story.id}: {candidate.package_name} {candidate.package_version} at "
        f"{candidate.path}, commit {candidate.commit} in repository {candidate.repository_id}. "
        f"Capability: {candidate.capability}. Rationale: {candidate.rationale}. "
        "The code is unreviewed and exclusive to this product; it awaits the owner's "
        "personal code and security review before any release."
        for candidate in candidates
    )
