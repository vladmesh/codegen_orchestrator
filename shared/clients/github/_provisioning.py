import httpx

from shared.log_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class ProvisioningMixin:
    """High-level repository provisioning (creates the repo and its project config)."""

    async def provision_project_repo(
        self,
        name: str,
        description: str = "",
        project_spec: dict | None = None,
    ) -> GitHubRepository:
        """Create repo with its initial project config.

        Org is auto-detected from GitHub App installation.

        Args:
            name: Repository name (will be sanitized to kebab-case)
            description: Repository description
            project_spec: Project specification to save as .project.yaml

        Returns:
            Created repository info
        """
        # 1. Auto-detect org from GitHub App installation
        installation = await self.get_first_org_installation()
        org = installation["org"]

        # 2. Sanitize repo name
        repo_name = name.lower().replace(" ", "-").replace("_", "-")

        # 3. Create repository
        try:
            repo = await self.create_repo(org, repo_name, description, private=True)
        except httpx.HTTPStatusError as exc:
            # Idempotency is a narrow HTTP contract: a 422 is accepted only if
            # a read proves that the repository actually exists.
            if exc.response.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
                raise
            try:
                repo = await self.get_repo(org, repo_name)
            except httpx.HTTPStatusError as lookup_exc:
                if lookup_exc.response.status_code == httpx.codes.NOT_FOUND:
                    raise exc from lookup_exc
                raise
            logger.info("github_repo_already_exists_using_existing", org=org, repo=repo_name)

        # 4. Add .project.yaml if spec provided
        if project_spec:
            import yaml

            config_content = yaml.dump(project_spec, default_flow_style=False, allow_unicode=True)
            await self.create_or_update_file(
                owner=org,
                repo=repo_name,
                path=".project.yaml",
                content=config_content,
                message="chore: add project configuration",
            )

        logger.info("project_repo_provisioned", org=org, repo=repo_name)

        return repo
