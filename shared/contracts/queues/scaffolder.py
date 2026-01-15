from shared.contracts.base import BaseMessage, BaseResult
from shared.contracts.dto.project import ServiceModule


class ScaffolderMessage(BaseMessage):
    """
    Initialize project structure.

    Responsibilities:
    1. Create remote repository (if not exists).
    2. Generate .project.yml config.
    3. Run copier template.
    4. Push initial commit.
    """

    project_id: str
    project_name: str
    repo_full_name: str  # org/repo format, e.g. "vladmesh/my-project"
    modules: list[ServiceModule]


class ScaffolderResult(BaseResult):
    """
    Scaffolder result.
    Stream: scaffolder:results
    """

    project_id: str
    repo_url: str
    files_generated: int = 0
