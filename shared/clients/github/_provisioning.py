import httpx

from shared.log_config import get_logger
from shared.schemas.github import GitHubRepository

logger = get_logger(__name__)


class ProvisioningMixin:
    """High-level repository provisioning (creates repo + sets secrets)."""

    async def provision_project_repo(
        self,
        name: str,
        description: str = "",
        project_spec: dict | None = None,
        secrets: dict[str, str] | None = None,
    ) -> GitHubRepository:
        """Create repo with initial config and secrets.

        Org is auto-detected from GitHub App installation.

        Args:
            name: Repository name (will be sanitized to kebab-case)
            description: Repository description
            project_spec: Project specification to save as .project.yaml
            secrets: Secrets to set in GitHub Actions (e.g., TELEGRAM_TOKEN)

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
        except httpx.HTTPStatusError as e:
            # Idempotency: Use existing repo if it already exists
            # GitHub API returns 422 Unprocessable Entity for existing repos
            if e.response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
                logger.info("github_repo_already_exists_using_existing", org=org, repo=repo_name)
                repo = await self.get_repo(org, repo_name)
            else:
                raise e
        except Exception as e:
            if "422" in str(e):  # Fallback for non-HTTPStatusError exceptions if any
                logger.info(
                    "github_repo_already_exists_using_existing_fallback", org=org, repo=repo_name
                )
                repo = await self.get_repo(org, repo_name)
            else:
                raise e

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

        # 5. Set secrets if provided
        if secrets:
            await self.set_repository_secrets(org, repo_name, secrets)

        logger.info(
            "project_repo_provisioned",
            org=org,
            repo=repo_name,
            secrets_count=len(secrets) if secrets else 0,
        )

        return repo
