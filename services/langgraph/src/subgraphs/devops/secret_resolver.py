"""SecretResolverNode — resolves secrets by generating, computing, and checking user-provided."""

from ipaddress import ip_address
import json
import os
import secrets as secrets_module
from urllib.parse import urlparse

import structlog

from shared.contracts.env_contract import CanonicalEnvContract
from shared.contracts.runtime_project import runtime_project_slug
from shared.crypto import decrypt_dict

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from .state import DevOpsState

logger = structlog.get_logger()

_MAX_TCP_PORT = 65535
_REPOSITORY_PATH_PARTS = 2


class SecretResolutionError(RuntimeError):
    """Raised when deploy secrets cannot be resolved from trusted state."""


class UnknownDerivedKeyError(SecretResolutionError):
    """Raised when an optional derived contract entry has no platform resolver."""


class TypedSecretResolutionError(SecretResolutionError):
    """A resolver error with a deploy outcome that callers must preserve."""

    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome


class SecretResolverNode(FunctionalNode):
    """Resolve secrets from the typed environment contract."""

    def __init__(self):
        super().__init__(node_id="secret_resolver")

    async def run(self, state: DevOpsState) -> dict:
        """Resolve all secrets from the required environment contract."""
        logger.info("secret_resolver_start")
        return await self._resolve_contract(state)

    async def _resolve_contract(self, state: DevOpsState) -> dict:
        """Resolve only production entries from a validated typed contract."""
        project_spec = state.get("project_spec")
        project_id = state.get("project_id")
        self._validate_project_context(project_id, project_spec)
        assert isinstance(project_id, str)
        assert isinstance(project_spec, dict)
        try:
            contract = CanonicalEnvContract.model_validate(state["environment_contract"])
        except ValueError as error:
            raise TypedSecretResolutionError(
                "environment_contract_invalid", "environment contract is invalid"
            ) from error

        encrypted = project_spec.get("config", {}).get("secrets", {})
        config_secrets = decrypt_dict(encrypted) if encrypted else {}
        provided_secrets = state.get("provided_secrets", {})
        secret_values: dict[str, str] = {}
        non_secret_values: dict[str, str] = {}
        generated: dict[str, str] = {}
        missing_user: list[str] = []

        for key, entry in contract.entries.items():
            if "production" not in entry.environments:
                continue
            try:
                if entry.source == "user_secret":
                    value = provided_secrets.get(key, config_secrets.get(key))
                    if value is None:
                        if entry.required:
                            missing_user.append(key)
                        continue
                    secret_values[key] = str(value)
                elif entry.source == "generated_secret":
                    value = config_secrets.get(key)
                    if value is None:
                        value = self._generate_infra_secret()
                        generated[key] = value
                    secret_values[key] = str(value)
                elif entry.source == "allocation":
                    selector = entry.service or entry.resource
                    assert selector is not None
                    allocation = self._find_allocation(state, selector)
                    if allocation is None:
                        raise TypedSecretResolutionError(
                            "allocation_missing", f"Missing allocation for {selector}"
                        )
                    self._store_contract_value(
                        key, str(allocation[1]), entry.sensitive, secret_values, non_secret_values
                    )
                elif entry.source == "derived":
                    self._store_contract_value(
                        key,
                        self._compute_secret(key, project_spec, state),
                        entry.sensitive,
                        secret_values,
                        non_secret_values,
                    )
                elif entry.source == "literal":
                    self._store_contract_value(
                        key,
                        self._dotenv_value(entry.value),
                        entry.sensitive,
                        secret_values,
                        non_secret_values,
                    )
            except TypedSecretResolutionError:
                raise
            except SecretResolutionError as error:
                if not entry.required and isinstance(error, UnknownDerivedKeyError):
                    logger.info("optional_environment_contract_entry_skipped", key=key)
                    continue
                raise TypedSecretResolutionError(
                    "environment_resolution_failed", str(error)
                ) from error

        if generated:
            try:
                await self._save_secrets_to_project(project_id, generated)
            except Exception as error:
                raise TypedSecretResolutionError(
                    "environment_resolution_failed", "Failed to persist generated secrets"
                ) from error
        logger.info(
            "environment_contract_resolved",
            secret_count=len(secret_values),
            non_secret_count=len(non_secret_values),
            missing_user_count=len(missing_user),
        )
        return {
            "secret_values": secret_values,
            "non_secret_values": non_secret_values,
            "missing_user_secrets": missing_user,
            "resolution_outcome": "waiting_for_user_secret" if missing_user else None,
        }

    @staticmethod
    def _store_contract_value(
        key: str,
        value: str,
        sensitive: bool,
        secret_values: dict[str, str],
        non_secret_values: dict[str, str],
    ) -> None:
        if sensitive:
            secret_values[key] = value
        else:
            non_secret_values[key] = value

    @staticmethod
    def _dotenv_value(value: object) -> str:
        """Render YAML scalar values using dotenv-compatible JSON literals."""
        if isinstance(value, bool):
            return json.dumps(value)
        return str(value)

    @staticmethod
    def _validate_project_context(project_id: str | None, project_spec: dict | None) -> None:
        """Validate project data required to persist and compute deploy secrets."""
        if not isinstance(project_id, str) or not project_id.strip() or project_id == "unknown":
            raise TypedSecretResolutionError(
                "environment_resolution_failed",
                "project_id is required for secret resolution",
            )
        if not isinstance(project_spec, dict):
            raise TypedSecretResolutionError(
                "environment_resolution_failed",
                "project context is required for secret resolution",
            )
        project_name = project_spec.get("name")
        if not isinstance(project_name, str) or not project_name.strip():
            raise TypedSecretResolutionError(
                "environment_resolution_failed",
                "project name is required for secret resolution",
            )
        try:
            runtime_project_slug(project_name)
        except ValueError as error:
            raise TypedSecretResolutionError("environment_resolution_failed", str(error)) from error

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

    @staticmethod
    def _generate_infra_secret() -> str:
        """Generate a random value for a generated contract secret."""
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
            return str(runtime_project_slug(project_spec["name"]))

        if key_upper == "PROJECT_NAME":
            return str(runtime_project_slug(project_spec["name"]))

        safe_project_id = state.get("project_id", "").replace("-", "_").lower()
        if key_upper == "POSTGRES_DB":
            return f"db_{safe_project_id}"
        if key_upper == "COMPOSE_PROJECT_NAME":
            return str(runtime_project_slug(project_spec["name"]))
        if key_upper == "ENABLED_MODULES":
            modules = project_spec.get("config", {}).get("modules", [])
            if not isinstance(modules, list) or not all(
                isinstance(module, str) for module in modules
            ):
                raise SecretResolutionError("project modules are invalid")
            return ",".join(modules)

        if key_upper in self._PORT_SERVICE_MAP:
            return self._resolve_port(key_upper, state)

        if key_upper.endswith("_IMAGE"):
            return self._resolve_docker_image(key_upper, state)

        raise UnknownDerivedKeyError(f"Unknown computed secret: {key}")

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
