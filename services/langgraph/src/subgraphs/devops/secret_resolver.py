"""SecretResolverNode — resolves secrets by generating, computing, and checking user-provided."""

from dataclasses import dataclass, field
from ipaddress import ip_address
import json
import os
import secrets as secrets_module
from urllib.parse import urlparse

import structlog

from shared.contracts.dto.run_result import MissingUserSecret
from shared.contracts.env_contract import (
    AllocationEntry,
    CanonicalEnvContract,
    DerivedEntry,
    EnvContractEntry,
    GeneratedSecretEntry,
    LiteralEntry,
    UserSecretEntry,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.crypto import decrypt_dict

from ...clients.api import api_client
from ...nodes.base import FunctionalNode
from ...runtime_identity import project_spec_runtime_slug
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

    def __init__(self, outcome: DeployOutcome, message: str):
        super().__init__(message)
        self.outcome = outcome


@dataclass
class _ResolvedValues:
    """Accumulates one deploy's resolved contract values."""

    secret_values: dict[str, str] = field(default_factory=dict)
    non_secret_values: dict[str, str] = field(default_factory=dict)
    generated: dict[str, str] = field(default_factory=dict)
    missing_user: list[MissingUserSecret] = field(default_factory=list)

    def store(self, key: str, value: str, sensitive: bool) -> None:
        """Route a resolved value to the secret or non-secret bucket."""
        if sensitive:
            self.secret_values[key] = value
        else:
            self.non_secret_values[key] = value


@dataclass(frozen=True)
class _ResolutionContext:
    """Trusted deploy context every entry handler resolves against."""

    project_id: str
    project_spec: dict
    state: DevOpsState
    config_secrets: dict
    provided_secrets: dict


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
        project_id, project_spec = self._validated_project_context(state)
        try:
            contract = CanonicalEnvContract.model_validate(state["environment_contract"])
        except ValueError as error:
            raise TypedSecretResolutionError(
                DeployOutcome.ENVIRONMENT_CONTRACT_INVALID, "environment contract is invalid"
            ) from error

        encrypted = project_spec.get("config", {}).get("secrets", {})
        context = _ResolutionContext(
            project_id=project_id,
            project_spec=project_spec,
            state=state,
            config_secrets=decrypt_dict(encrypted) if encrypted else {},
            provided_secrets=state.get("provided_secrets", {}),
        )
        resolved = _ResolvedValues()

        for key, entry in contract.entries.items():
            if "production" not in entry.environments:
                continue
            try:
                self._resolve_entry(key, entry, context, resolved)
            except TypedSecretResolutionError:
                raise
            except SecretResolutionError as error:
                if not entry.required and isinstance(error, UnknownDerivedKeyError):
                    logger.info("optional_environment_contract_entry_skipped", key=key)
                    continue
                raise TypedSecretResolutionError(
                    DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED, str(error)
                ) from error

        if resolved.generated:
            try:
                await self._save_secrets_to_project(project_id, resolved.generated)
            except Exception as error:
                raise TypedSecretResolutionError(
                    DeployOutcome.ENVIRONMENT_RESOLUTION_FAILED,
                    "Failed to persist generated secrets",
                ) from error
        logger.info(
            "environment_contract_resolved",
            secret_count=len(resolved.secret_values),
            non_secret_count=len(resolved.non_secret_values),
            missing_user_count=len(resolved.missing_user),
        )
        return {
            "secret_values": resolved.secret_values,
            "non_secret_values": resolved.non_secret_values,
            "missing_user_secrets": [m.model_dump() for m in resolved.missing_user],
            "resolution_outcome": (
                DeployOutcome.WAITING_FOR_USER_SECRET if resolved.missing_user else None
            ),
        }

    def _resolve_entry(
        self,
        key: str,
        entry: EnvContractEntry,
        context: _ResolutionContext,
        resolved: _ResolvedValues,
    ) -> None:
        """Dispatch one contract entry to the handler for its declared type.

        An entry type with no handler is a contract change the resolver has not
        learned yet, so it fails loudly instead of dropping the variable from
        the deployed environment.
        """
        match entry:
            case UserSecretEntry():
                self._resolve_user_secret(key, entry, context, resolved)
            case GeneratedSecretEntry():
                self._resolve_generated_secret(key, context, resolved)
            case AllocationEntry():
                self._resolve_allocation(key, entry, context, resolved)
            case DerivedEntry():
                resolved.store(
                    key,
                    self._compute_secret(key, context.project_spec, context.state),
                    entry.sensitive,
                )
            case LiteralEntry():
                resolved.store(key, self._dotenv_value(entry.value), entry.sensitive)
            case _:
                raise TypedSecretResolutionError(
                    DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
                    f"no resolver for environment contract entry type {type(entry).__name__}",
                )

    @staticmethod
    def _resolve_user_secret(
        key: str,
        entry: UserSecretEntry,
        context: _ResolutionContext,
        resolved: _ResolvedValues,
    ) -> None:
        """Take a user-provided secret, or record it as still missing."""
        value = context.provided_secrets.get(key, context.config_secrets.get(key))
        if value is None:
            if entry.required:
                resolved.missing_user.append(
                    MissingUserSecret(key=key, description=entry.description)
                )
            return
        resolved.secret_values[key] = str(value)

    def _resolve_generated_secret(
        self, key: str, context: _ResolutionContext, resolved: _ResolvedValues
    ) -> None:
        """Reuse the persisted platform secret, or generate one to persist."""
        value = context.config_secrets.get(key)
        if value is None:
            value = self._generate_infra_secret()
            resolved.generated[key] = value
        resolved.secret_values[key] = str(value)

    def _resolve_allocation(
        self,
        key: str,
        entry: AllocationEntry,
        context: _ResolutionContext,
        resolved: _ResolvedValues,
    ) -> None:
        """Resolve the port held by the entry's allocation selector."""
        allocation = self._find_allocation(context.state, entry.selector)
        if allocation is None:
            raise TypedSecretResolutionError(
                DeployOutcome.ALLOCATION_MISSING, f"Missing allocation for {entry.selector}"
            )
        resolved.store(key, str(allocation[1]), entry.sensitive)

    @staticmethod
    def _dotenv_value(value: object) -> str:
        """Render YAML scalar values using dotenv-compatible JSON literals."""
        if isinstance(value, bool):
            return json.dumps(value)
        return str(value)

    @staticmethod
    def _validated_project_context(state: DevOpsState) -> tuple[str, dict]:
        """Return the project data required to persist and compute deploy secrets."""
        project_id = state.get("project_id")
        project_spec = state.get("project_spec")
        if not isinstance(project_id, str) or not project_id.strip() or project_id == "unknown":
            raise SecretResolutionError("project_id is required for secret resolution")
        if not isinstance(project_spec, dict):
            raise SecretResolutionError("project context is required for secret resolution")
        project_slug = project_spec.get("slug")
        if not isinstance(project_slug, str) or not project_slug.strip():
            raise SecretResolutionError("project slug is required for secret resolution")
        return project_id, project_spec

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
            return project_spec_runtime_slug(project_spec)

        if key_upper == "PROJECT_NAME":
            return project_spec_runtime_slug(project_spec)

        safe_project_id = state.get("project_id", "").replace("-", "_").lower()
        if key_upper == "POSTGRES_DB":
            return f"db_{safe_project_id}"
        if key_upper == "COMPOSE_PROJECT_NAME":
            return project_spec_runtime_slug(project_spec)
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
