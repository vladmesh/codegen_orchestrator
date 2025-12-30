"""Environment analyzer node for DevOps subgraph.

Analyzes .env.example files and classifies variables using LLM.
"""

import json
import re

from langchain_core.messages import AIMessage, SystemMessage
import structlog

from shared.clients.github import GitHubAppClient

from ...clients.api import api_client
from ...config.agent_config_cache import agent_config_cache
from ...llm.factory import LLMFactory
from ...schemas.api_types import ProjectInfo, get_repo_url
from .state import DevOpsState

logger = structlog.get_logger()


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


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Parse owner and repo name from repository URL.

    Args:
        repo_url: GitHub repository URL

    Returns:
        Tuple of (owner, repo) or None if parsing fails
    """
    try:
        parts = repo_url.rstrip("/").split("/")
        return parts[-2], parts[-1]
    except Exception:
        return None


def _parse_env_variables(content: str) -> list[str]:
    """Parse environment variable names from .env file content.

    Args:
        content: Raw content of .env.example file

    Returns:
        List of variable names
    """
    variables = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            variables.append(key)
    return variables


async def _fetch_env_content(owner: str, repo: str) -> str | None:
    """Fetch .env.example or .env.template content from GitHub.

    Args:
        owner: Repository owner
        repo: Repository name

    Returns:
        File content or None if not found
    """
    github = GitHubAppClient()
    content = await github.get_file_contents(owner, repo, ".env.example")
    if not content:
        content = await github.get_file_contents(owner, repo, ".env.template")
    return content


def _parse_llm_response(response_text: str) -> dict | None:
    """Parse JSON from LLM response.

    Handles both raw JSON and markdown code blocks.

    Args:
        response_text: Raw LLM response text

    Returns:
        Parsed dict or None if parsing fails
    """
    # Try markdown code block first
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try raw JSON
        json_match = re.search(r"\{[^{}]*\}", response_text, re.DOTALL)
        json_str = json_match.group(0) if json_match else None

    if not json_str:
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


async def _classify_variables_with_llm(
    variables: list[str],
    project_context: str,
) -> tuple[dict, AIMessage | None]:
    """Classify environment variables using LLM.

    Args:
        variables: List of variable names
        project_context: Context string for the LLM

    Returns:
        Tuple of (analysis dict, AI message) or (fallback dict, None) on error
    """
    try:
        config = await agent_config_cache.get("devops")
        llm = LLMFactory.create_llm(config)
    except Exception as e:
        logger.error("devops_config_fetch_failed", error=str(e))
        return dict.fromkeys(variables, "user"), None

    prompt = ENV_ANALYZER_PROMPT.format(
        project_context=project_context,
        env_variables="\n".join(f"- {v}" for v in variables),
    )

    response = await llm.ainvoke([SystemMessage(content=prompt)])
    response_text = response.content

    env_analysis = _parse_llm_response(response_text)
    if env_analysis is None:
        logger.warning(
            "env_analysis_json_parse_failed",
            response=response_text[:200],
        )
        env_analysis = dict.fromkeys(variables, "user")

    return env_analysis, response


async def env_analyzer_run(state: DevOpsState) -> dict:
    """Analyze .env.example and classify each variable.

    This is the main entry point for the env_analyzer node.
    """
    project_id = state.get("project_id")
    logger.info("env_analyzer_start", project_id=project_id)

    # Validate project_id
    if not project_id:
        return {
            "errors": ["No project_id provided to DevOps subgraph"],
            "env_analysis": {},
        }

    # Get project info
    project: ProjectInfo | None = await api_client.get_project(project_id)
    if not project:
        return {
            "errors": [f"Project {project_id} not found"],
            "env_analysis": {},
        }

    # Get and parse repository URL
    repo_url = get_repo_url(project)
    if not repo_url:
        return {
            "errors": ["No repository URL found for project"],
            "env_analysis": {},
        }

    parsed = _parse_repo_url(repo_url)
    if not parsed:
        return {
            "errors": [f"Invalid repository URL format: {repo_url}"],
            "env_analysis": {},
        }
    owner, repo = parsed

    # Fetch .env.example content
    try:
        content = await _fetch_env_content(owner, repo)
    except Exception as e:
        logger.error("env_analyzer_failed", error=str(e), exc_info=True)
        return {
            "errors": [f"Environment analysis failed: {e}"],
            "env_analysis": {},
        }

    # Handle missing env file
    if not content:
        logger.info("no_env_file_found", project_id=project_id)
        return {
            "env_variables": [],
            "env_analysis": {},
            "messages": [AIMessage(content="No .env.example found. Proceeding with deployment.")],
        }

    # Parse variables
    variables = _parse_env_variables(content)
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

    # Classify variables with LLM
    env_analysis, response = await _classify_variables_with_llm(variables, project_context)

    logger.info(
        "env_analyzer_complete",
        project_id=project_id,
        variables_count=len(variables),
        analysis=env_analysis,
    )

    result = {
        "env_variables": variables,
        "env_analysis": env_analysis,
    }
    if response:
        result["messages"] = [response]

    return result
