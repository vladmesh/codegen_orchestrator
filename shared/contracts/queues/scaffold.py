from typing import Literal

from shared.contracts.base import BaseMessage


class ScaffoldMessage(BaseMessage):
    """Trigger scaffolding for a project repository.

    Published by scheduler for both new (draft) and existing (active) projects.
    Consumed by scaffolder service.

    Modes:
        full: Full scaffold — copier + make setup + git push (new projects).
        ensure: Verify workspace exists; if missing, clone + setup (existing projects).
    """

    project_id: str
    repository_id: str
    user_id: str
    template_repo: str  # e.g. "gh:vladmesh/service-template"
    project_name: str  # sanitized name for copier
    modules: str  # comma-separated, e.g. "backend,tg_bot"
    task_description: str = ""
    mode: Literal["full", "ensure"] = "full"
