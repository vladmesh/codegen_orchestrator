"""SecretResolverNode — resolves secrets by generating, computing, and checking user-provided."""

import os
import secrets as secrets_module

import structlog

from shared.crypto import decrypt_dict

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from .env_groups import resolve_with_groups
from .state import DevOpsState

logger = structlog.get_logger()


class SecretResolverNode(FunctionalNode):
    """Resolve secrets by generating infra, computing context-based, and checking user-provided."""

    def __init__(self):
        super().__init__(node_id="secret_resolver")

    async def run(self, state: DevOpsState) -> dict:
        """Resolve all secrets based on classification."""
        logger.info("secret_resolver_start")

        env_analysis = state.get("env_analysis", {})
        provided_secrets = state.get("provided_secrets", {})
        project_spec = state.get("project_spec") or {}
        project_id = state.get("project_id") or "unknown"

        # Normalize project_id for DB names
        safe_project_id = project_id.replace("-", "_").lower()

        # Get previously saved secrets from project config (decrypt at rest)
        config_secrets = project_spec.get("config", {}).get("secrets", {})
        config_secrets = decrypt_dict(config_secrets) if config_secrets else {}

        resolved = {}
        missing_user = []
        newly_generated = {}  # Track new infra secrets to save

        # Phase 1: Split infra vars into cached vs uncached
        uncached_infra: set[str] = set()
        for var, var_type in env_analysis.items():
            if var_type == "infra":
                if var in config_secrets:
                    resolved[var] = config_secrets[var]
                    logger.debug("secret_reused", var=var)
                else:
                    uncached_infra.add(var)

        # Phase 2: Resolve uncached infra via groups (coherent passwords)
        if uncached_infra:
            grouped, remaining = resolve_with_groups(uncached_infra, safe_project_id)
            resolved.update(grouped)
            newly_generated.update(grouped)
            logger.debug("secrets_grouped", count=len(grouped), vars=sorted(grouped))

            # Fallback for remaining vars not covered by any group
            for var in remaining:
                resolved[var] = self._generate_infra_secret(var, safe_project_id)
                newly_generated[var] = resolved[var]
                logger.debug("secret_generated", var=var)

        # Phase 3: Computed and user secrets (unchanged)
        for var, var_type in env_analysis.items():
            if var_type == "computed":
                resolved[var] = self._compute_secret(var, project_spec, state)

            elif var_type == "user":
                # Check if user provided it via:
                # 1. provided_secrets (passed directly)
                # 2. project_spec.config.secrets (saved to DB via save_project_secret)
                if var in provided_secrets:
                    resolved[var] = provided_secrets[var]
                elif var in config_secrets:
                    resolved[var] = config_secrets[var]
                else:
                    missing_user.append(var)

        # Phase 4: Inject PO-provided secrets not in .env.example
        # env_analysis only covers vars from .env.example. PO may have set
        # secrets (e.g. ADMIN_TELEGRAM_ID) that the worker used in code but
        # didn't add to .env.example. Ensure they always reach the .env.
        for var, val in config_secrets.items():
            if var not in resolved:
                resolved[var] = val
                logger.info("secret_injected_from_config", var=var)

        # Save newly generated secrets to project config for reuse on redeploy
        if newly_generated and project_id != "unknown":
            await self._save_secrets_to_project(project_id, newly_generated)

        logger.info(
            "secret_resolver_complete",
            resolved_count=len(resolved),
            missing_user_count=len(missing_user),
            missing_user=missing_user,
            newly_generated_count=len(newly_generated),
        )

        return {
            "resolved_secrets": resolved,
            "missing_user_secrets": missing_user,
        }

    def _find_allocation(self, state: DevOpsState, service_name: str) -> tuple[str, int] | None:
        """Look up allocated server IP and port for a service.

        Searches allocated_resources by matching service_name field.

        Returns:
            (server_ip, port) tuple or None if not found.
        """
        resources = state.get("allocated_resources", {})
        for alloc in resources.values():
            if isinstance(alloc, dict) and alloc.get("service_name") == service_name:
                ip = alloc.get("server_ip", "localhost")
                port = alloc.get("port", 8000)
                return ip, port
        return None

    def _generate_infra_secret(self, key: str, project_id: str) -> str:
        """Generate infrastructure secret value (fallback for vars not covered by groups)."""
        key_upper = key.upper()

        if "SECRET" in key_upper or ("KEY" in key_upper and "API" not in key_upper):
            return secrets_module.token_urlsafe(32)

        # Default random for unknown infra
        return secrets_module.token_urlsafe(32)

    def _compute_secret(self, key: str, project_spec: dict, state: DevOpsState) -> str:  # noqa: PLR0911
        """Compute context-based secret value."""
        key_upper = key.upper()

        if key_upper == "APP_NAME":
            return project_spec.get("name", "app").replace(" ", "_").lower()

        elif key_upper in {"APP_ENV", "ENVIRONMENT"}:
            return "production"

        elif key_upper == "DEBUG":
            return "false"

        elif key_upper == "PROJECT_NAME":
            return project_spec.get("name", "project")

        elif key_upper == "POSTGRES_HOST":
            return "db"  # Docker service name

        elif key_upper == "POSTGRES_PORT":
            return "5432"

        elif key_upper == "POSTGRES_REQUIRE_SSL":
            return "false"

        elif key_upper in {"BACKEND_PORT", "FRONTEND_PORT", "TG_BOT_PORT"}:
            # Map VAR_PORT -> service name in allocator
            service_map = {
                "BACKEND_PORT": "backend",
                "FRONTEND_PORT": "frontend",
                "TG_BOT_PORT": "tg_bot",
            }
            service = service_map.get(key_upper, "backend")
            alloc = self._find_allocation(state, service)
            if alloc:
                return str(alloc[1])
            return "8000"

        elif key_upper in {"BACKEND_API_URL", "API_URL", "API_BASE_URL", "BACKEND_URL"}:
            # Inter-service URL: use docker service name, not external IP
            return "http://backend:8000"

        # Docker images from self-hosted registry
        elif key_upper.endswith("_IMAGE"):
            registry_host = os.getenv("ORCHESTRATOR_HOSTNAME")
            if not registry_host:
                raise RuntimeError("ORCHESTRATOR_HOSTNAME is not set")
            repo_info = state.get("repo_info") or {}
            repo_url = repo_info.get("html_url", "")
            if repo_url:
                # Parse: https://github.com/org/repo -> org/repo
                parts = repo_url.rstrip("/").split("/")
                owner = parts[-2] if len(parts) > 1 else "unknown"
                repo = parts[-1] if parts else "unknown"

                # Derive service name: BACKEND_IMAGE -> backend, TG_BOT_IMAGE -> tg-bot
                service = key_upper.replace("_IMAGE", "").lower().replace("_", "-")

                return f"{registry_host}/{owner}/{repo}-{service}:latest"
            return f"{registry_host}/unknown/unknown-service:latest"

        # Default: project name
        return project_spec.get("name", "value")

    async def _save_secrets_to_project(self, project_id: str, secrets: dict) -> None:
        """Save newly generated secrets to project config for reuse on redeploy.

        Uses the atomic merge endpoint to avoid race conditions.

        Args:
            project_id: Project ID
            secrets: Dict of secret_name -> secret_value to save
        """
        try:
            await api_client.merge_secrets(project_id, secrets)

            logger.info(
                "secrets_saved_to_project",
                project_id=project_id,
                secrets_count=len(secrets),
                secret_names=list(secrets.keys()),
            )
        except Exception as e:
            # Log but don't fail - secrets will be regenerated next time
            logger.error(
                "save_secrets_failed",
                project_id=project_id,
                error=str(e),
                error_type=type(e).__name__,
            )
