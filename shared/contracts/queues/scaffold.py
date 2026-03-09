from shared.contracts.base import BaseMessage


class ScaffoldMessage(BaseMessage):
    """Trigger scaffolding for a new project repository.

    Published by scheduler when project.status == draft and has stories.
    Consumed by scaffolder service.
    """

    project_id: str
    repository_id: str
    user_id: str
    template_repo: str  # e.g. "gh:project-factory-organization/service-template"
    project_name: str  # sanitized name for copier
    modules: str  # comma-separated, e.g. "backend,tg_bot"
    task_description: str = ""
