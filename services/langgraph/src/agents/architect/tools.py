"""Architect agent tools.

Tools for the architect ReAct agent to decompose stories into tasks.
All tools use the shared LanggraphAPIClient singleton.

Task chaining: create_task auto-chains tasks sequentially: each new task
is blocked by the previous one. The LLM doesn't need to track task IDs
or manage dependencies.

Story lifecycle is deliberately absent: the architect consumer moves the story
to IN_PROGRESS around this agent's run, and an agent tool that moved it too
gave one code path two Story transitions for the same story.

Planning identity is deliberately absent from every mutating tool schema:
story/project ownership plus brief/attempt identity arrive through `InjectedState`,
so the model can neither redirect a task to another story/project nor invent an
attempt. A run that is not planning under a brief carries `None` for the brief
fields, and then `create_task` sends the ordinary non-brief shape.

Catalog selection uses `plan_install(name)`: the injected catalog snapshot owns
package, recommended library and default-binding identities in a typed INSTALL.
`create_task` continues ordinary engineering planning, but refuses prose kit
installation recipes and supplied artifacts. No model field controls command
vectors, component sources, operation leases or engineering accounting.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import replace
import io
import re
import shlex
from typing import Annotated

from framework.catalog import parse_catalog
from framework.cli import _parser as kit_cli_parser
import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import ValidationError
import structlog

from shared.contracts.dto.product_brief import RequirementCoverageCreate
from shared.contracts.dto.task import TaskCreate, TaskStatus, TaskType

from ...catalog_install import InstallRefusal, plan_install_payload
from ...clients.api import api_client
from ...kit_catalog import installable

logger = structlog.get_logger(__name__)

# Auto-chaining state: tracks the last created task ID per story.
# Reset between architect invocations (module is long-lived but each graph
# invocation starts fresh via reset_task_chain).
_last_task_id: dict[str, str] = {}


def reset_task_chain() -> None:
    """Reset auto-chaining state. Call before each architect invocation."""
    _last_task_id.clear()


#: One `kit add` invocation, up to where a command written in prose ends: a closing
#: backtick, a line end, a shell separator, a comma or parenthesis, or a full stop.
_KIT_ADD = re.compile(r"(?<![\w-])kit\s+add\b(?P<arguments>(?:[^`\n;|&,().]|\.(?=\S))*)")
#: A wheel file named anywhere, outside any `kit add` too.
_WHEEL_FILE = re.compile(r"[\w.+-]+\.whl\b", re.IGNORECASE)
#: A kit package distribution installed past `kit add`, e.g. `uv add codegen-kit-reminders`.
_DISTRIBUTION_INSTALL = re.compile(
    r"\b(?:pip|uv)\b[^\n]*?\b(?:install|add)\b[^\n]*?\bcodegen[-_]kit[-_]", re.IGNORECASE
)
_ARTIFACT_REFUSAL = (
    "a kit package is installed only as `kit add <catalog name>`, which resolves the "
    "release from the kit catalog itself; the task must not install from a wheel "
    "file or a built artifact (`--wheel`, `.whl`)"
)


def _kit_add_arguments(arguments: str) -> argparse.Namespace | str:
    """One invocation as the pinned kit CLI parses it, or why it cannot be parsed.

    The parser is the kit's own (`framework.cli._parser`), so the guard accepts exactly
    the spellings the installed `kit` accepts, abbreviations such as `--wh` included.
    """
    try:
        tokens = shlex.split(arguments)
    except ValueError as error:
        return f"`kit add{arguments}` cannot be split into arguments ({error})"
    try:
        with contextlib.redirect_stderr(io.StringIO()) as complaint:
            parsed, _rest = kit_cli_parser().parse_known_args(["add", *tokens])
    except SystemExit:
        lines = complaint.getvalue().strip().splitlines()
        reason = lines[-1] if lines else "invalid"
        return f"`kit add{arguments}` is not a command the kit accepts ({reason})"
    return parsed


def package_install_refusal(text: str, catalog: list[str] | None) -> str | None:
    """Why a task's text may not install the package it names, or `None` when it may.

    A planning lint that keeps the task on the catalog route, not a security boundary.
    Each `kit add` invocation is tokenised with `shlex` and parsed by the pinned kit's
    own CLI parser: it is refused when it sets the wheel option (in any spelling the
    parser accepts) or names a package outside the live catalog the run was briefed
    with (`None`: the catalog was unavailable, so nothing is installable). A wheel file
    named anywhere and a pip or uv install of a kit distribution are refused too. Text
    that installs nothing passes. A command assembled from shell variables or aliases,
    which the parser never sees, is out of its reach.
    """
    if _WHEEL_FILE.search(text):
        return _ARTIFACT_REFUSAL
    if _DISTRIBUTION_INSTALL.search(text):
        return (
            "a kit package is installed only as `kit add <catalog name>`, not by installing "
            "its distribution with pip or uv"
        )
    invocations = [match.group("arguments") for match in _KIT_ADD.finditer(text)]
    if not invocations:
        return None
    names = []
    for arguments in invocations:
        parsed = _kit_add_arguments(arguments)
        if isinstance(parsed, str):
            return (
                f"{parsed}; write the install as `kit add <catalog name>` in backticks, on its own"
            )
        if parsed.wheel is not None:
            return _ARTIFACT_REFUSAL
        names.append(parsed.name)
    if catalog is None:
        return (
            "the kit package catalog was unavailable when this plan started, so no package "
            "can be planned in this run: create the task without `kit add`, or return the "
            "requirement it covers"
        )
    unknown = sorted({name for name in names if name not in catalog})
    if unknown:
        listed = ", ".join(sorted(catalog)) or "none"
        return (
            f"`kit add` names {', '.join(repr(name) for name in unknown)}, which the kit "
            f"package catalog does not list (installable packages: {listed}); write "
            "`kit add <name>` with a listed name, or plan the capability without a package"
        )
    return None


@tool
async def get_story(story_id: str) -> dict:
    """Get a story by ID. Returns title, description, status, and project_id."""
    story = await api_client.get_story(story_id)
    return story.model_dump(mode="json")


@tool
async def get_project_spec(project_id: str, detail: str = "") -> dict:
    """Get project overview, file tree, and spec summaries.

    By default returns a compact overview: project metadata, file tree,
    module list, and specs_summary (model names, domains, events).
    This is usually enough for task decomposition.

    Use `detail` only when the summary is insufficient for a specific decision:
        detail="models" : full model definitions with fields and types
        detail="events" : full event definitions
        detail="domains": full domain operations with methods and paths

    Args:
        project_id: Project ID.
        detail: Optional detail level. Empty for summary, or one of:
            "models", "events", "domains".
    """
    project = await api_client.get_project(project_id)
    if project is None:
        return {"error": f"Project {project_id} not found"}

    result = project.model_dump(mode="json")
    config = project.config or {}
    specs_summary = config.get("specs_summary", {})

    # Always include tree and basic info
    result["tree"] = config.get("tree")

    # Strip noisy fields from the config in the result dict
    result_config = result.get("config") or {}
    for key in ("secrets", "env_hints", "specs_summary"):
        result_config.pop(key, None)

    if not detail:
        # Compact summary: just names and counts
        compact = {}
        if specs_summary.get("models"):
            compact["models"] = [m["name"] for m in specs_summary["models"]]
        if specs_summary.get("events"):
            compact["events"] = [e["name"] for e in specs_summary["events"]]
        if specs_summary.get("domains"):
            compact["domains"] = [
                f"{d['service']}/{d['domain']} ({len(d['operations'])} ops)"
                for d in specs_summary["domains"]
            ]
        result["specs"] = compact
    elif detail == "models":
        result["specs_detail"] = {"models": specs_summary.get("models", [])}
    elif detail == "events":
        result["specs_detail"] = {"events": specs_summary.get("events", [])}
    elif detail == "domains":
        result["specs_detail"] = {"domains": specs_summary.get("domains", [])}
    else:
        result["specs"] = {"error": f"Unknown detail: {detail}. Use: models, events, domains"}

    return result


@tool
async def get_tasks_by_story(story_id: str) -> list[dict]:
    """Get all existing tasks for a story. Use to check what work already exists."""
    tasks = await api_client.get_tasks_by_story(story_id)
    return [t.model_dump(mode="json") for t in tasks]


@tool
async def plan_install(
    name: str,
    story_id: Annotated[str, InjectedState("story_id")],
    project_id: Annotated[str, InjectedState("project_id")],
    kit_install_snapshot: Annotated[dict | None, InjectedState("kit_install_snapshot")],
    planning_attempt_id: Annotated[str | None, InjectedState("planning_attempt_id")] = None,
) -> dict:
    """Select a catalog package on an existing backend,tg_bot product.

    Creates exactly one mechanical INSTALL task, including curated libraries and
    the default binding. No model or engineering Run executes installation.
    Use this for explicit catalog selections; never split the closure into coding tasks.
    """
    if kit_install_snapshot is None:
        return {"error": "catalog_unavailable"}
    project = await api_client.get_project(project_id)
    repository = await api_client.get_primary_repository(project_id)
    if (
        project is None
        or repository is None
        or project.status == "draft"
        or not {"backend", "tg_bot"}.issubset(project.modules)
    ):
        return {"error": "product_incompatible: requires an existing backend,tg_bot product"}
    try:
        snapshot = replace(
            installable(
                parse_catalog(kit_install_snapshot["catalog"]),
                kit_install_snapshot["source"],
                kit_install_snapshot["core_version"],
            ),
            bindings=kit_install_snapshot["bindings"],
            manifests=kit_install_snapshot["manifests"],
        )
        payload = plan_install_payload(snapshot, name, "3.12.0")
        body = TaskCreate(
            title=f"Install catalog package {name}",
            type=TaskType.INSTALL,
            project_id=project_id,
            repository_id=repository.id,
            story_id=story_id,
            install=payload,
            status=TaskStatus.TODO,
            created_by="architect",
            blocked_by_task_id=_last_task_id.get(story_id),
            planning_attempt_id=planning_attempt_id,
            description=f"Install {name} through scaffolder; review the generated story PR.",
            acceptance_criteria="The selected package, recommended libraries and default binding "
            "are installed, regenerated and validated in the product.",
        )
    except (InstallRefusal, ValueError) as error:
        return {"error": str(error)}
    result = await api_client.create_task(body.model_dump(mode="json"))
    _last_task_id[story_id] = result.id
    return result.model_dump(mode="json")


@tool
async def create_task(
    title: str,
    description: str,
    type: str,
    acceptance_criteria: str,
    story_id: Annotated[str, InjectedState("story_id")],
    project_id: Annotated[str, InjectedState("project_id")],
    planning_attempt_id: Annotated[str | None, InjectedState("planning_attempt_id")] = None,
    kit_catalog_packages: Annotated[list[str] | None, InjectedState("kit_catalog_packages")] = None,
) -> dict:
    """Create a new task for a story.

    Tasks are automatically chained: each new task is blocked by the previous
    one created for the same story. Just call create_task in the right order —
    dependencies are handled for you.

    Explicit catalog installation uses plan_install(name), which owns one typed
    INSTALL task and its package/library/default-binding closure. This tool
    refuses kit install prose; use it for unrelated ordinary feature work.

    Args:
        title: Short task title.
        description: What needs to be done.
        type: One of: create, feature, fix, refactor.
        acceptance_criteria: How to verify the task is done.
    """
    refusal = package_install_refusal(f"{description}\n{acceptance_criteria}", kit_catalog_packages)
    if refusal is not None:
        logger.warning("architect_task_package_refused", title=title, detail=refusal)
        return {"error": f"task {title!r} was refused: {refusal}"}

    if _KIT_ADD.search(f"{description}\n{acceptance_criteria}"):
        return {
            "error": f"task {title!r} was refused: catalog_install_requires_plan_install; "
            "call plan_install(name) for one typed mechanical install task"
        }

    blocked_by = _last_task_id.get(story_id)

    task_data = {
        "title": title,
        "description": description,
        "type": type,
        "acceptance_criteria": acceptance_criteria,
        "story_id": story_id,
        "project_id": project_id,
        "status": TaskStatus.TODO,
        "blocked_by_task_id": blocked_by,
        "created_by": "architect",
    }
    if planning_attempt_id is not None:
        # Planning under a Product Brief: the API creates the task unadmitted
        # under this attempt, and only `POST /product-briefs/{id}/admit`
        # releases it. Absent otherwise, so an ordinary task is created with the
        # shape it has always been created with.
        task_data["planning_attempt_id"] = planning_attempt_id
    result = await api_client.create_task(task_data)

    # Track for auto-chaining
    task_id = result.id
    if task_id:
        _last_task_id[story_id] = task_id

    logger.info(
        "architect_task_created",
        task_id=task_id,
        title=title,
        blocked_by=blocked_by,
        planning_attempt_id=planning_attempt_id,
    )
    return result.model_dump(mode="json")


def _refusal_detail(error: httpx.HTTPStatusError) -> str:
    """What the API refused, in the words it refused it with.

    The architect's next move depends on which refusal this was: an unknown
    requirement id is a different repair from a disposition that named both a
    task and a reason: so the detail is handed back to the model rather than
    flattened into "failed".
    """
    try:
        body = error.response.json()
    except ValueError:
        return error.response.text or str(error)
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else str(body)


@tool
async def record_requirement_coverage(
    requirement_id: str,
    task_id: str = "",
    returned_reason: str = "",
    brief_id: Annotated[str | None, InjectedState("product_brief_id")] = None,
    planning_attempt_id: Annotated[str | None, InjectedState("planning_attempt_id")] = None,
) -> dict:
    """Record how you disposed of ONE must-requirement of the Product Brief.

    Exactly one disposition per requirement id, and exactly one of the two
    arguments: the id of the task that covers it, or the reason it is being
    returned undone. Neither is not an answer and both is two answers.

    Nothing in this story's plan is released until every must-requirement id has
    a disposition recorded here, so call this once per requirement: including
    the ones you are returning.

    Args:
        requirement_id: The must-requirement id, exactly as it was given to you.
        task_id: The task created for it, when a task covers it.
        returned_reason: Why it is being returned, when no task covers it.
    """
    if not brief_id or not planning_attempt_id:
        return {
            "error": (
                "this story is not planned under a Product Brief planning attempt; "
                "there is no requirement coverage to record"
            )
        }
    try:
        coverage = RequirementCoverageCreate(
            requirement_id=requirement_id,
            planning_attempt_id=planning_attempt_id,
            task_id=task_id or None,
            returned_reason=returned_reason or None,
        )
    except ValidationError as invalid:
        return {"error": f"invalid disposition for {requirement_id}: {invalid}"}
    try:
        recorded = await api_client.record_requirement_coverage(brief_id, coverage)
    except httpx.HTTPStatusError as refused:
        detail = _refusal_detail(refused)
        logger.warning(
            "architect_requirement_coverage_refused",
            brief_id=brief_id,
            requirement_id=requirement_id,
            status_code=refused.response.status_code,
            detail=detail,
        )
        return {"error": f"coverage for {requirement_id} was refused: {detail}"}
    logger.info(
        "architect_requirement_coverage_recorded",
        brief_id=brief_id,
        requirement_id=requirement_id,
        task_id=coverage.task_id,
        returned=coverage.returned_reason is not None,
    )
    return recorded.model_dump(mode="json")


@tool
async def update_acceptance_criteria(project_id: str, acceptance_criteria: str) -> dict:
    """Update the repository's acceptance criteria for regression testing.

    Call this AFTER creating all tasks. Pass the COMPLETE updated list of
    acceptance criteria: not just the new ones. Read the current criteria
    first (returned in the response), add checks for new functionality from
    this story, and remove checks for deleted functionality.

    Format: one check per line, starting with "- ". Each check is concrete and
    uses only what "What QA Can Check" in your instructions names; a behaviour
    that needs something QA never does is checked by its observable afterwards:
        - GET /health returns 200
        - GET /api/cities lists Moscow after the bot is told "add city Moscow"
        - Telegram: /start responds with welcome message

    A behaviour the product runs on a schedule is named in its own form, which
    the platform reads rather than an executor: QA fires the behaviour itself
    and judges it on what follows THEN:
        - FIRE JOB daily_digest THEN a digest message is delivered to the owner
        - FIRE JOB daily_digest WITH {"languages":["ru","en"]} THEN a digest per configured language

    The name is character for character the one the product's
    `services/<service>/manifest.yaml` declares under `jobs_schema`, the
    arguments after WITH are one JSON object its declared schema accepts, and
    what follows THEN asserts a capability rather than a sample: "a digest per
    configured language" is a check, "there is a Russian item this week"
    makes QA red on a quiet week. Where typed settings configure the behaviour,
    the observable comes from those confirmed values, not from the story prose.
    Add such a line only for a behaviour the product actually declares.

    Args:
        project_id: Project ID (same as used in create_task).
        acceptance_criteria: The FULL updated acceptance criteria text.
    """
    repo = await api_client.get_primary_repository(project_id)
    if not repo:
        return {"error": f"No repository found for project {project_id}"}

    updated = await api_client.update_repository(
        repo.id, {"acceptance_criteria": acceptance_criteria}
    )
    logger.info(
        "architect_acceptance_criteria_updated",
        repo_id=repo.id,
        criteria_length=len(acceptance_criteria),
    )
    return {
        "repo_id": updated.id,
        "acceptance_criteria": updated.acceptance_criteria,
    }


def get_architect_tools() -> list:
    """Return all architect tools."""
    return [
        get_story,
        get_project_spec,
        get_tasks_by_story,
        create_task,
        plan_install,
        record_requirement_coverage,
        update_acceptance_criteria,
    ]
