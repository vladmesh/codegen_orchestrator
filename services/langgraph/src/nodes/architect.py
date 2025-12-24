"""Architect agent node.

Creates project structure using Factory.ai Droid in an isolated container.
Generates high-level architecture without business logic.
"""

import logging
import os

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from ..clients.github import GitHubAppClient
from ..clients.worker_spawner import request_spawn
from ..tools.github import create_github_repo, get_github_token

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Architect, the project structuring agent in the codegen orchestrator.

Your job:
1. Analyze the project requirements to determine complexity.
2. Create a new GitHub repository for the project.
3. Initialize the project structure using `service-template` (via copier).
4. Generate domain specifications (YAML files) based on project requirements.
5. Set up the basic infrastructure (Docker, CI/CD).

## Available tools:
- create_github_repo(name, description): Create a new private GitHub repository
- get_github_token(repo_full_name): Get a token for git operations
- set_project_complexity(complexity: str): Set the project complexity to "simple" or "complex"

## Project Complexity:
- **Simple**: The project is very simple, with business logic fitting in a few dozen lines. No complex workflows, no heavy external integrations.
    - Example: A simple CRUD service, a basic bot that just echoes or saves to DB.
    - Action: Set complexity to "simple". The worker will implement the logic directly.
- **Complex**: The project has non-trivial business logic, complex workflows, or requires careful design.
    - Example: An e-commerce system, a complex orchestration workflow, a system with many integrations.
    - Action: Set complexity to "complex". The Developer agent will be called next to implement the logic.

## Workflow:
1. Assess complexity and call `set_project_complexity`.
2. Create a GitHub repository using `create_github_repo`.
3. Get a token using `get_github_token`.
4. Finally, report the created repository details.

## Guidelines:
- Repository name should match project name (snake_case)
- Include project description from the spec
- Do NOT implement business logic - only structure (UNLESS complexity is "simple", but the worker handles that, you just set the flag)
- Focus on: domain specs, models, API routes (no controllers)

## Documentation:
The architectural framework documentation is available at `/home/vlad/projects/service-template`.
- **Framework Guide**: `/home/vlad/projects/service-template/AGENTS.md`
- **Manifesto**: `/home/vlad/projects/service-template/docs/MANIFESTO.md`
If you are unsure about module names or structure, refer to these files.

## CRITICAL INSTRUCTIONS:
- You must **IMMEDIATELY** call `set_project_complexity` and `create_github_repo`.
- **DO NOT** write any conversational text (like "Okay", "I will do that", "Starting").
- **DO NOT** stop to ask for confirmation.
- **ONLY** output tool calls until the repository is created and the worker is spawned.
- Only after `architect_spawn_worker` has finished (which you trigger by creating repo + token) should you optionally report success. But since you are the Architect node, just call the tools.

## Current Project Info:
{project_info}

## Allocated Resources:
{allocated_resources}
"""

@tool
def set_project_complexity(complexity: str):
    """Set the project complexity level.
    
    Args:
        complexity: "simple" or "complex"
    """
    return complexity


# LLM with tools
llm = ChatOpenAI(model="gpt-4o", temperature=0)
tools = [create_github_repo, get_github_token, set_project_complexity]
llm_with_tools = llm.bind_tools(tools)

# Tool mapping
tools_map = {tool.name: tool for tool in tools}


async def run(state: dict) -> dict:
    """Run architect agent.
    
    Creates GitHub repo and prepares for code generation.
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec", {})
    allocated_resources = state.get("allocated_resources", {})
    
    # Build context for LLM
    project_info = f"""
Name: {project_spec.get('name', 'unknown')}
Description: {project_spec.get('description', 'No description')}
Modules: {project_spec.get('modules', [])}
Entry Points: {project_spec.get('entry_points', [])}
"""
    
    system_content = SYSTEM_PROMPT.format(
        project_info=project_info,
        allocated_resources=allocated_resources,
    )
    
    llm_messages = [SystemMessage(content=system_content)]
    llm_messages.extend(messages)
    
    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)
    
    return {
        "messages": [response],
        "current_agent": "architect",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Architect LLM."""
    messages = state.get("messages", [])
    last_message = messages[-1]
    
    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}
    
    tool_results = []
    repo_info = state.get("repo_info", {})
    project_complexity = state.get("project_complexity")
    
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_func = tools_map.get(tool_name)
        
        if tool_func:
            try:
                result = await tool_func.ainvoke(tool_call["args"])
                
                # Track created repo
                if tool_name == "create_github_repo" and result:
                    repo_info = result
                
                # Track complexity
                if tool_name == "set_project_complexity":
                    project_complexity = result
                
                tool_results.append(
                    ToolMessage(
                        content=f"Result: {result}",
                        tool_call_id=tool_call["id"],
                    )
                )
            except Exception as e:
                logger.exception(f"Tool {tool_name} failed: {e}")
                tool_results.append(
                    ToolMessage(
                        content=f"Error: {e!s}",
                        tool_call_id=tool_call["id"],
                    )
                )
        else:
            tool_results.append(
                ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            )
    
    return {
        "messages": tool_results,
        "repo_info": repo_info,
        "project_complexity": project_complexity,
    }


async def spawn_factory_worker(state: dict) -> dict:
    """Spawn Factory.ai worker to generate project structure.
    
    This node is called after repo is created and token is obtained.
    It spawns a Sysbox container with Factory.ai Droid to generate code.
    """
    repo_info = state.get("repo_info", {})
    project_spec = state.get("project_spec", {})
    project_complexity = state.get("project_complexity", "complex")  # Default to complex
    
    if not repo_info:
        return {
            "messages": [AIMessage(content="❌ No repository info found. Cannot spawn worker.")],
            "errors": state.get("errors", []) + ["No repository info for architect worker"],
        }
    
    repo_full_name = repo_info.get("full_name")
    if not repo_full_name:
        return {
            "messages": [AIMessage(content="❌ Repository full_name not found.")],
            "errors": state.get("errors", []) + ["Repository full_name missing"],
        }
    
    # Get fresh token for the repo
    github_client = GitHubAppClient()
    owner, repo = repo_full_name.split("/")
    
    try:
        token = await github_client.get_token(owner, repo)
    except Exception as e:
        logger.exception(f"Failed to get GitHub token: {e}")
        return {
            "messages": [AIMessage(content=f"❌ Failed to get GitHub token: {e}")],
            "errors": state.get("errors", []) + [str(e)],
        }
    
    # If simple, add implementation instructions directly
    extra_instructions = ""
    if project_complexity == "simple":
        extra_instructions = """
5.  **Implement Business Logic (SIMPLE PROJECT)**:
    - Since this is a simple project, please implement the business logic immediately.
    - Connect the generated API routers to your implementation.
    - Ensure tests pass.
    - THIS IS THE FINAL STEP, so make sure it works.
"""

    task_content = f"""# Project: {project_spec.get('name', 'project')}

## Description
{project_spec.get('description', 'No description provided')}

## Requirements
- Modules: {', '.join(project_spec.get('modules', []))}
- Entry Points: {', '.join(project_spec.get('entry_points', []))}

## Task
Initialize this repository using the `service-template` framework.

1.  **Initialize Project via Copier**:
    - The template is located at `gh:vladmesh/service-template`.
    - Use `copier` to generate the project structure.
    - Run: `copier copy gh:vladmesh/service-template . --data project_name={project_spec.get('name', 'project')} --data modules={','.join(project_spec.get('modules', ['backend']))} --trust` (adjust modules as needed).
    - If `copier` is not installed, install it: `pip install copier`.

2.  **Define Domain Specifications**:
    - Create YAML specifications in `shared/spec/` (or `domains/` if you prefer, but template uses `shared/spec`).
    - Define entities, aggregates, and services.

3.  **Setup Configuration**:
    - Ensure `.env` is created from `.env.example`.
    - Ensure `docker-compose.yml` is present (generated by copier).

4.  **Push Changes**:
    - You **MUST** commit and push your changes to the repository.
    - Run: `git add .`
    - Run: `git commit -m "Initial project structure"` (if not already committed)
    - Run: `git push`

{extra_instructions}

## Important
- **DO NOT** create a manual structure. You **MUST** use the `service-template`.
- Read `AGENTS.md` (if available in template) for context.
- All code should be async-ready (Python 3.12+).

## Commit Message
Initial project structure for {project_spec.get('name', 'project')} using service-template
"""
    
    logger.info(f"Spawning Factory worker for {repo_full_name}")
    
    result = await request_spawn(
        repo=repo_full_name,
        github_token=token,
        task_content=task_content,
        task_title=f"Initial structure for {project_spec.get('name', 'project')}",
        model=os.getenv("FACTORY_MODEL", "claude-sonnet-4-5-20250929"),
    )
    
    if result.success:
        message = f"""✅ Project structure created successfully!

Repository: {repo_info.get('html_url')}
Commit: {result.commit_sha or 'N/A'}

The repository now has:
- Domain specifications
- Project configuration
- Docker setup
- CI/CD workflow

Next step: Developer agent will implement business logic.
"""
        return {
            "messages": [AIMessage(content=message)],
            "architect_complete": True,
        }
    else:
        return {
            "messages": [AIMessage(content=f"❌ Factory worker failed:\n\n{result.output[-500:]}")],
            "errors": state.get("errors", []) + ["Factory worker failed"],
        }

