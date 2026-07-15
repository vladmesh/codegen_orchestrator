"""SecretResolverNode — resolves secrets by generating, computing, and checking user-provided."""

from ipaddress import ip_address
import os
import secrets as secrets_module
from urllib.parse import urlparse

import structlog

from shared.crypto import decrypt_dict

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from .env_groups import resolve_with_groups
from .state import DevOpsState

logger = structlog.get_logger()

_MAX_TCP_PORT = 65535
_REPOSITORY_PATH_PARTS = 2


class SecretResolutionError(RuntimeError):
    """Raised when deploy secrets cannot be resolved from trusted state."""


class SecretResolverNode(FunctionalNode):
    """Resolve secrets by generating infra, computing context-based, and checking user-provided."""

    def __init__(self):
        super().__init__(node_id="secret_resolver")

    async def run(self, state: DevOpsState) -> dict:
        """Resolve all secrets based on classification."""
        logger.info("secret_resolver_start")

        env_analysis = state.get("env_analysis", {})
        provided_secrets = state.get("provided_secrets", {})
        project_spec = state.get("project_spec")
        project_id = state.get("project_id")
        self._validate_project_context(project_id, project_spec)
        assert isinstance(project_id, str)
        assert isinstance(project_spec, dict)

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
        if newly_generated:
            try:
                await self._save_secrets_to_project(project_id, newly_generated)
            except Exception as error:
                raise SecretResolutionError("Failed to persist generated secrets") from error

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

    @staticmethod
    def _validate_project_context(project_id: str | None, project_spec: dict | None) -> None:
        """Validate project data required to persist and compute deploy secrets."""
        if not isinstance(project_id, str) or not project_id.strip() or project_id == "unknown":
            raise SecretResolutionError("project_id is required for secret resolution")
        if not isinstance(project_spec, dict):
            raise SecretResolutionError("project context is required for secret resolution")
        project_name = project_spec.get("name")
        if not isinstance(project_name, str) or not project_name.strip():
            raise SecretResolutionError("project name is required for secret resolution")

    def _find_allocation(self, state: DevOpsState, service_name: str) -> tuple[str, int] | None:
        """Look up allocated server IP and port for a service.

        Searches allocated_resources by matching service_name field.

        Returns:
            (server_ip, port) tuple or None if not found.
        """
        resources = state.get("allocated_resources", {})
        matches = [
            alloc
            for alloc in resources.values()
            if isinstance(alloc, dict) and alloc.get("service_name") == service_name
        ]
        if len(matches) > 1:
            raise SecretResolutionError(f"Ambiguous allocation for service {service_name}")

        for alloc in matches:
            ip = alloc.get("server_ip")
            port = alloc.get("port")
            if not isinstance(ip, str) or not ip.strip() or ip.lower() == "localhost":
                raise SecretResolutionError(
                    f"Invalid allocation for service {service_name}: server_ip is required"
                )
            try:
                ip_address(ip)
            except ValueError as error:
                raise SecretResolutionError(
                    f"Invalid allocation for service {service_name}: server_ip is invalid"
                ) from error
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= _MAX_TCP_PORT
            ):
                raise SecretResolutionError(
                    f"Invalid allocation for service {service_name}: port is required"
                )
            return ip, port
        return None

    def _generate_infra_secret(self, key: str, project_id: str) -> str:
        """Generate infrastructure secret value (fallback for vars not covered by groups)."""
        key_upper = key.upper()

        if "SECRET" in key_upper or ("KEY" in key_upper and "API" not in key_upper):
            return secrets_module.token_urlsafe(32)

        # Default random for unknown infra
        return secrets_module.token_urlsafe(32)

    # Static key → value mappings for computed secrets
    _STATIC_SECRETS: dict[str, str] = {
        "APP_ENV": "production",
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_REQUIRE_SSL": "false",
        "BACKEND_API_URL": "http://backend:8000",
        "API_URL": "http://backend:8000",
        "API_BASE_URL": "http://backend:8000",
        "BACKEND_URL": "http://backend:8000",
    }

    # Port keys → allocator service names
    _PORT_SERVICE_MAP: dict[str, str] = {
        "BACKEND_PORT": "backend",
        "FRONTEND_PORT": "frontend",
        "TG_BOT_PORT": "tg_bot",
        "POSTGRES_HOST_PORT": "postgres",
        "REDIS_HOST_PORT": "redis",
    }

    def _compute_secret(self, key: str, project_spec: dict, state: DevOpsState) -> str:
        """Compute context-based secret value."""
        key_upper = key.upper()

        if key_upper in self._STATIC_SECRETS:
            return self._STATIC_SECRETS[key_upper]

        if key_upper == "APP_NAME":
            return project_spec["name"].replace(" ", "_").lower()

        if key_upper == "PROJECT_NAME":
            return project_spec["name"]

        if key_upper in self._PORT_SERVICE_MAP:
            return self._resolve_port(key_upper, state)

        if key_upper.endswith("_IMAGE"):
            return self._resolve_docker_image(key_upper, state)

        raise SecretResolutionError(f"Unknown computed secret: {key}")

    def _resolve_port(self, key_upper: str, state: DevOpsState) -> str:
        """Resolve port from resource allocator."""
        service = self._PORT_SERVICE_MAP[key_upper]
        alloc = self._find_allocation(state, service)
        if alloc:
            return str(alloc[1])
        raise SecretResolutionError(f"Missing allocation for service {service}")

    def _resolve_docker_image(self, key_upper: str, state: DevOpsState) -> str:
        """Build Docker image URL from self-hosted registry."""
        registry_host = os.getenv("ORCHESTRATOR_HOSTNAME")
        if not registry_host:
            raise SecretResolutionError("ORCHESTRATOR_HOSTNAME is not set")
        repo_info = state.get("repo_info") or {}
        repo_url = repo_info.get("html_url")
        if not isinstance(repo_url, str):
            raise SecretResolutionError(
                "repository metadata is required for Docker image resolution"
            )

        parsed_url = urlparse(repo_url)
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or len(path_parts) != _REPOSITORY_PATH_PARTS
            or not all(path_parts)
        ):
            raise SecretResolutionError(
                "repository metadata is malformed for Docker image resolution"
            )

        owner, repo = path_parts
        service = key_upper.removesuffix("_IMAGE").lower().replace("_", "-")
        return f"{registry_host}/{owner}/{repo}-{service}:latest"

    async def _save_secrets_to_project(self, project_id: str, secrets: dict) -> None:
        """Save newly generated secrets to project config for reuse on redeploy.

        Uses the atomic merge endpoint to avoid race conditions.

        Args:
            project_id: Project ID
            secrets: Dict of secret_name -> secret_value to save
        """
        await api_client.merge_secrets(project_id, secrets)
        logger.info(
            "secrets_saved_to_project",
            project_id=project_id,
            secrets_count=len(secrets),
            secret_names=list(secrets.keys()),
        )
