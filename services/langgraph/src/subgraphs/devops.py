"""DevOps Subgraph.

Handles intelligent secret classification and deployment.
Returns missing user secrets to Product Owner if needed.

Topology:
    START → env_analyzer → secret_resolver → readiness_check
                                                  ↓
                                  [if missing] → END (return to PO)
                                  [if ready]  → deployer → END
"""

import secrets as secrets_module
from typing import Annotated, Any

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
import structlog
from typing_extensions import TypedDict

from shared.clients.github import GitHubAppClient

from ..clients.api import api_client
from ..config.agent_config_cache import agent_config_cache
from ..llm.factory import LLMFactory
from ..nodes.base import FunctionalNode
from ..tools.devops_tools import run_ansible_deploy

logger = structlog.get_logger()


def _merge_errors(left: list[str], right: list[str]) -> list[str]:
    """Reducer that merges error lists without duplicates."""
    seen = set(left)
    result = list(left)
    for err in right:
        if err not in seen:
            result.append(err)
            seen.add(err)
    return result


class DevOpsState(TypedDict):
    """State for the DevOps subgraph."""

    # Messages (conversation history for LLM)
    messages: Annotated[list, add_messages]

    # Input (from parent graph)
    project_id: str | None
    project_spec: dict | None
    allocated_resources: dict
    repo_info: dict | None
    provided_secrets: dict  # secrets provided by PO

    # Internal (analysis results)
    env_variables: list[str]  # Raw list of env vars from .env.example
    env_analysis: dict  # {var_name: "infra"|"computed"|"user"}
    resolved_secrets: dict  # generated/computed secrets

    # Output (returned to parent)
    missing_user_secrets: list[str]
    deployment_result: dict | None
    deployed_url: str | None
    errors: Annotated[list[str], _merge_errors]


# ============================================================
# ENV ANALYZER NODE (LLM)
# ============================================================

ENV_ANALYZER_PROMPT = """You are an expert DevOps engineer analyzing env variables.

Given a list of environment variables from .env.example, classify each as one of:

1. **INFRA** - Internal infrastructure secrets that should be auto-generated:
   - Database URLs (DATABASE_URL, POSTGRES_*, REDIS_URL, MONGO_URL)
   - Internal service URLs (BACKEND_URL if internal)
   - Secret keys (APP_SECRET_KEY, JWT_SECRET, SESSION_SECRET)
   - Internal ports and hosts

2. **COMPUTED** - Values derived from project context:
   - APP_NAME, APP_ENV, PROJECT_NAME
   - BACKEND_API_URL (if external-facing)
   - Domain names based on project

3. **USER** - External API keys that only the user can provide:
   - TELEGRAM_BOT_TOKEN
   - OPENAI_API_KEY, ANTHROPIC_API_KEY
   - STRIPE_KEY, STRIPE_SECRET
   - Any third-party service credentials
   - OAuth client IDs/secrets for external services

When in doubt, classify as USER (safer to ask than to generate wrong value).

Project context:
{project_context}

Environment variables to classify:
{env_variables}

Respond with a JSON object mapping each variable to its type:
```json
{{
  "DATABASE_URL": "infra",
  "REDIS_URL": "infra",
  "APP_NAME": "computed",
  "TELEGRAM_BOT_TOKEN": "user"
}}
```

Only respond with the JSON, no other text."""


async def env_analyzer_run(state: DevOpsState) -> dict:
    """Analyze .env.example and classify each variable."""
    logger.info(
        "env_analyzer_start",
        project_id=state.get("project_id"),
    )

    project_id = state.get("project_id")
    if not project_id:
        return {
            "errors": ["No project_id provided to DevOps subgraph"],
            "env_analysis": {},
        }

    # Get project info
    project = await api_client.get_project(project_id)
    if not project:
        return {
            "errors": [f"Project {project_id} not found"],
            "env_analysis": {},
        }

    # Get repository URL
    repo_url = project.get("repository_url") or project.get("config", {}).get("repository_url")
    if not repo_url:
        return {
            "errors": ["No repository URL found for project"],
            "env_analysis": {},
        }

    # Parse owner/repo from URL
    try:
        parts = repo_url.rstrip("/").split("/")
        repo = parts[-1]
        owner = parts[-2]
    except Exception:
        return {
            "errors": [f"Invalid repository URL format: {repo_url}"],
            "env_analysis": {},
        }

    # Get .env.example content
    github = GitHubAppClient()
    try:
        content = await github.get_file_contents(owner, repo, ".env.example")
        if not content:
            content = await github.get_file_contents(owner, repo, ".env.template")

        if not content:
            logger.info("no_env_file_found", project_id=project_id)
            return {
                "env_variables": [],
                "env_analysis": {},
                "messages": [
                    AIMessage(content="No .env.example found. Proceeding with deployment.")
                ],
            }

        # Parse variables
        variables = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key = line.split("=", 1)[0].strip()
                variables.append(key)

        if not variables:
            logger.info("no_env_variables_found", project_id=project_id)
            return {
                "env_variables": [],
                "env_analysis": {},
            }

        # Build project context for LLM
        project_context = f"""
Project Name: {project.get("name", "unknown")}
Repository: {repo_url}
Allocated Resources: {state.get("allocated_resources", {})}
"""

        # Get LLM config for devops agent
        try:
            config = await agent_config_cache.get("devops")
            llm = LLMFactory.create_llm(config)
        except Exception as e:
            logger.error("devops_config_fetch_failed", error=str(e))
            # Fallback: classify everything as "user" for safety
            return {
                "env_variables": variables,
                "env_analysis": dict.fromkeys(variables, "user"),
                "errors": [f"LLM config error, defaulting all to user: {e}"],
            }

        # Build prompt
        prompt = ENV_ANALYZER_PROMPT.format(
            project_context=project_context,
            env_variables="\n".join(f"- {v}" for v in variables),
        )

        # Invoke LLM
        response = await llm.ainvoke([SystemMessage(content=prompt)])
        response_text = response.content

        # Parse JSON from response
        import json
        import re

        # Extract JSON from markdown code block if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON
            json_match = re.search(r"\{[^{}]*\}", response_text, re.DOTALL)
            json_str = json_match.group(0) if json_match else "{}"

        try:
            env_analysis = json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning(
                "env_analysis_json_parse_failed",
                response=response_text[:200],
            )
            # Fallback: classify everything as "user"
            env_analysis = dict.fromkeys(variables, "user")

        logger.info(
            "env_analyzer_complete",
            project_id=project_id,
            variables_count=len(variables),
            analysis=env_analysis,
        )

        return {
            "env_variables": variables,
            "env_analysis": env_analysis,
            "messages": [response],
        }

    except Exception as e:
        logger.error("env_analyzer_failed", error=str(e), exc_info=True)
        return {
            "errors": [f"Environment analysis failed: {e}"],
            "env_analysis": {},
        }


# ============================================================
# SECRET RESOLVER NODE (Functional)
# ============================================================


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

        resolved = {}
        missing_user = []

        for var, var_type in env_analysis.items():
            if var_type == "infra":
                # Generate infrastructure secrets
                resolved[var] = self._generate_infra_secret(var, safe_project_id)

            elif var_type == "computed":
                # Compute from project context
                resolved[var] = self._compute_secret(var, project_spec, state)

            elif var_type == "user":
                # Check if user provided it via:
                # 1. provided_secrets (passed directly)
                # 2. project_spec.config.secrets (saved to DB via save_project_secret)
                config_secrets = project_spec.get("config", {}).get("secrets", {})
                if var in provided_secrets:
                    resolved[var] = provided_secrets[var]
                elif var in config_secrets:
                    resolved[var] = config_secrets[var]
                else:
                    missing_user.append(var)

        logger.info(
            "secret_resolver_complete",
            resolved_count=len(resolved),
            missing_user_count=len(missing_user),
            missing_user=missing_user,
        )

        return {
            "resolved_secrets": resolved,
            "missing_user_secrets": missing_user,
        }

    def _generate_infra_secret(self, key: str, project_id: str) -> str:
        """Generate infrastructure secret value."""
        key_upper = key.upper()

        if key_upper == "REDIS_URL":
            return "redis://redis:6379/0"

        elif key_upper == "DATABASE_URL":
            db_pass = secrets_module.token_urlsafe(16)
            db_name = f"db_{project_id}"
            return f"postgresql://postgres:{db_pass}@postgres:5432/{db_name}"

        elif "SECRET" in key_upper or ("KEY" in key_upper and "API" not in key_upper):
            return secrets_module.token_urlsafe(32)

        elif key_upper == "POSTGRES_USER":
            return "postgres"

        elif key_upper == "POSTGRES_PASSWORD":
            return secrets_module.token_urlsafe(16)

        elif key_upper == "POSTGRES_DB":
            return f"db_{project_id}"

        # Default random for unknown infra
        return secrets_module.token_urlsafe(32)

    def _compute_secret(self, key: str, project_spec: dict, state: DevOpsState) -> str:
        """Compute context-based secret value."""
        key_upper = key.upper()

        if key_upper == "APP_NAME":
            return project_spec.get("name", "app").replace(" ", "_").lower()

        elif key_upper in {"APP_ENV", "ENVIRONMENT"}:
            return "production"

        elif key_upper == "PROJECT_NAME":
            return project_spec.get("name", "project")

        elif key_upper in {"BACKEND_API_URL", "API_URL"}:
            # Use allocated resources if available
            resources = state.get("allocated_resources", {})
            if resources:
                first_resource = list(resources.values())[0]
                if isinstance(first_resource, dict):
                    ip = first_resource.get("server_ip", "localhost")
                    port = first_resource.get("port", 8000)
                    return f"http://{ip}:{port}"
            return "http://localhost:8000"

        # Default: project name
        return project_spec.get("name", "value")


secret_resolver_node = SecretResolverNode()


# ============================================================
# READINESS CHECK NODE (Functional)
# ============================================================


class ReadinessCheckNode(FunctionalNode):
    """Check if all user secrets are provided."""

    def __init__(self):
        super().__init__(node_id="readiness_check")

    async def run(self, state: DevOpsState) -> dict:
        """Check deployment readiness."""
        missing = state.get("missing_user_secrets", [])

        if missing:
            logger.info(
                "readiness_check_missing_secrets",
                missing=missing,
            )
            return {
                "messages": [
                    AIMessage(
                        content=f"Missing user secrets: {', '.join(missing)}. "
                        "Please provide these secrets to continue deployment."
                    )
                ],
            }

        logger.info("readiness_check_ready")
        return {}


readiness_check_node = ReadinessCheckNode()


# ============================================================
# DEPLOYER NODE (Functional)
# ============================================================


class DeployerNode(FunctionalNode):
    """Execute Ansible deployment."""

    def __init__(self):
        super().__init__(node_id="deployer")

    async def run(self, state: DevOpsState) -> dict:
        """Execute deployment with resolved secrets."""
        logger.info("deployer_start", project_id=state.get("project_id"))

        project_id = state.get("project_id")
        resolved_secrets = state.get("resolved_secrets", {})

        if not project_id:
            return {
                "deployment_result": {"status": "failed", "error": "No project_id"},
                "errors": ["No project_id for deployment"],
            }

        try:
            # Use the existing run_ansible_deploy tool
            result = await run_ansible_deploy.ainvoke(
                {
                    "project_id": project_id,
                    "secrets": resolved_secrets,
                }
            )

            logger.info(
                "deployer_complete",
                project_id=project_id,
                status=result.get("status"),
                deployed_url=result.get("deployed_url"),
            )

            if result.get("status") == "success":
                # Update project status to 'active' (system-managed)
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"status": "active"},
                )
                return {
                    "deployment_result": result,
                    "deployed_url": result.get("deployed_url"),
                    "messages": [
                        AIMessage(
                            content=f"Deployment successful! URL: {result.get('deployed_url')}"
                        )
                    ],
                }

            # Deployment failed - update status to 'error'
            await api_client.patch(
                f"/projects/{project_id}",
                json={"status": "error"},
            )
            return {
                "deployment_result": result,
                "errors": [result.get("error", "Deployment failed")],
                "messages": [AIMessage(content=f"Deployment failed: {result.get('error')}")],
            }

        except Exception as e:
            logger.error("deployer_failed", error=str(e), exc_info=True)
            # Update status to 'error' on exception
            try:
                await api_client.patch(
                    f"/projects/{project_id}",
                    json={"status": "error"},
                )
            except Exception as status_err:
                logger.warning("status_update_failed", error=str(status_err))
            return {
                "deployment_result": {"status": "error", "error": str(e)},
                "errors": [f"Deployment error: {e}"],
            }


deployer_node = DeployerNode()


# ============================================================
# ROUTING FUNCTIONS
# ============================================================


def route_after_env_analyzer(state: DevOpsState) -> str:
    """Route after env analysis."""
    # If errors, go to end
    if state.get("errors"):
        return END

    # Always proceed to secret resolver
    return "secret_resolver"


def route_after_secret_resolver(state: DevOpsState) -> str:
    """Route after secret resolution."""
    # Always proceed to readiness check
    return "readiness_check"


def route_after_readiness_check(state: DevOpsState) -> str:
    """Route after readiness check.

    - If missing secrets → END (return to PO)
    - If ready → deployer
    """
    missing = state.get("missing_user_secrets", [])

    if missing:
        logger.info(
            "route_to_end_missing_secrets",
            missing_count=len(missing),
        )
        return END

    return "deployer"


# ============================================================
# SUBGRAPH BUILDER
# ============================================================


def create_devops_subgraph() -> Any:
    """Create the DevOps subgraph.

    Topology:
        START → env_analyzer → secret_resolver → readiness_check
                                                      ↓
                                      [if missing] → END
                                      [if ready]  → deployer → END
    """
    graph = StateGraph(DevOpsState)

    # Add nodes
    graph.add_node("env_analyzer", env_analyzer_run)
    graph.add_node("secret_resolver", secret_resolver_node.run)
    graph.add_node("readiness_check", readiness_check_node.run)
    graph.add_node("deployer", deployer_node.run)

    # Edges
    graph.add_edge(START, "env_analyzer")

    graph.add_conditional_edges(
        "env_analyzer",
        route_after_env_analyzer,
        {
            "secret_resolver": "secret_resolver",
            END: END,
        },
    )

    graph.add_conditional_edges(
        "secret_resolver",
        route_after_secret_resolver,
        {
            "readiness_check": "readiness_check",
        },
    )

    graph.add_conditional_edges(
        "readiness_check",
        route_after_readiness_check,
        {
            "deployer": "deployer",
            END: END,
        },
    )

    graph.add_edge("deployer", END)

    return graph.compile()
