"""What the platform's own records say the order became, judged without guessing.

Each function reads records the controller fetched — API rows, typed results and
read-only repository facts — and returns one observation: its status and a
bounded, non-secret detail. A fact the records do not carry is `unknown`, never
assumed; only an explicit contradiction is `failed`. A typed status alone proves
nothing a provenance chain has to prove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from pydantic import ValidationError

from shared.contracts.dto.capability_preview import CapabilityPlan, CapabilityRoute
from shared.contracts.dto.catalog_install import (
    PRODUCT_GLUE_OWNER,
    CatalogInstall,
    InstallOperation,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome

from .codegen_api import typed_deploy_result, typed_qa_result
from .evidence import ObservationStatus
from .repository_evidence import CompareFacts, PullRequestFacts, WorkflowRunFacts

MODULE_ROUTES = frozenset({CapabilityRoute.MODULE, CapabilityRoute.MODULE_WITH_GLUE})
_TERMINAL_RUNS = frozenset({RunStatus.COMPLETED.value, RunStatus.FAILED.value})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}")
#: GitHub's compare relations in which the head contains the base.
_CONTAINS = frozenset({"ahead", "identical"})


@dataclass(frozen=True)
class Finding:
    status: ObservationStatus
    detail: Any = None


def observed(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.OBSERVED, detail)


def failed(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.FAILED, detail)


def unknown(detail: Any = None) -> Finding:
    return Finding(ObservationStatus.UNKNOWN, detail)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def brief_frozen(brief: dict | None) -> Finding:
    if brief is None:
        return unknown("the story is not backed by a Product Brief")
    detail = {
        "brief_id": brief.get("id"),
        "revision": brief.get("revision"),
        "confirmed_at": brief.get("confirmed_at"),
        "language": (brief.get("content") or {}).get("language"),
    }
    if not brief.get("confirmed_at"):
        return failed(detail)
    return observed(detail)


def plan_routes(plan: CapabilityPlan | None) -> Finding:
    if plan is None:
        return failed("the brief carries no capability plan: a from-scratch order")
    detail = {
        "preview_id": plan.preview_id,
        "capabilities": [
            {
                "request_id": capability.request_id,
                "capability_id": capability.capability_id,
                "route": capability.route.value,
                "install": None
                if capability.install is None
                else {
                    "package": capability.install.package.name,
                    "version": capability.install.package.version,
                    "core_version": capability.install.core_version,
                    "catalog_digest": capability.install.catalog_digest,
                },
            }
            for capability in plan.capabilities
        ],
    }
    module = [item for item in plan.capabilities if item.route in MODULE_ROUTES]
    if not module or any(item.install is None for item in module):
        return failed(detail)
    return observed(detail)


def preview_after_allowlist(
    preview: dict | None, allowlist_applied_at: str | None, project_id: str
) -> Finding:
    created = parse_time((preview or {}).get("created_at"))
    applied = parse_time(allowlist_applied_at)
    detail = {
        "preview_created_at": (preview or {}).get("created_at"),
        "allowlist": allowlist_applied_at,
    }
    if preview is not None and str(preview.get("project_id")) != project_id:
        return failed({**detail, "correlation": "a preview of another project"})
    if created is None or applied is None:
        return unknown(detail)
    return observed(detail) if created > applied else failed(detail)


# --- the install and what engineering did after it --------------------------------


def typed_install(task: dict) -> tuple[CatalogInstall, InstallOperation] | None:
    try:
        return (
            CatalogInstall.model_validate(task.get("install")),
            InstallOperation.model_validate(task.get("install_operation")),
        )
    except ValidationError:
        return None


def _operation_view(task: dict) -> dict:
    operation = task.get("install_operation") or {}
    preflight = operation.get("preflight") or {}
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "operation_id": operation.get("id"),
        "state": operation.get("state"),
        "cycle_started_at": operation.get("cycle_started_at"),
        "base_sha": operation.get("base_sha"),
        "head_sha": operation.get("head_sha"),
        "preflight_status": preflight.get("status"),
        "verification": operation.get("verification"),
    }


def _install_contradiction(task: dict, install: CatalogInstall, op: InstallOperation) -> str | None:
    if task.get("status") != TaskStatus.DONE.value or op.state != "published":
        return "the install is not a published operation"
    if op.preflight is None or op.verification is None:
        return None
    if op.preflight.status == "incompatible" or op.preflight.provenance_mismatch(install):
        return "the preflight is not this install's admitted result"
    verification = op.verification
    installed = verification.distributions.get(install.package.name) or (
        verification.distributions.get(install.package.distribution)
    )
    if (
        verification.core_version != install.core_version
        or verification.tooling_commit != install.tooling_commit
        or installed != install.package.version
    ):
        return "the verification names another closure"
    return None


def install_tasks(tasks: list[dict]) -> list[dict]:
    return [task for task in tasks if task.get("type") == TaskType.INSTALL.value]


def install_chain(  # noqa: C901, PLR0911 - one ordered chain, each link its own verdict
    tasks: list[dict],
    engineering_runs: list[dict],
    pull_request: PullRequestFacts | None,
    published: dict[str, CompareFacts | None],
    install_to_head: CompareFacts | None,
) -> tuple[Finding, str | None]:
    """Each install published mechanically on the scaffold, before any engineering.

    The chain starts at the pull request's base — the default-branch commit the
    story branched from, which for a fresh order is the scaffold — and every
    install's typed operation must start exactly where the previous one ended,
    with its own admitted preflight and matching verification, its published
    commits read from the repository, no engineering run on its task, and the
    pull request's head containing the last install. Returns the finding and the
    last install head, the base of the engineering delta.
    """
    installs = install_tasks(tasks)
    install_ids = {task.get("id") for task in installs}
    model_on_install = sorted(
        run["id"] for run in engineering_runs if run.get("task_id") in install_ids
    )
    detail: dict[str, Any] = {
        "operations": [_operation_view(task) for task in installs],
        "engineering_runs_on_install_tasks": model_on_install,
        "scaffold_baseline": None if pull_request is None else pull_request.base_sha,
        "published_commits": {
            key: None if facts is None else list(facts.commits) for key, facts in published.items()
        },
    }
    if not installs or model_on_install:
        return failed(detail), None
    typed = {}
    for task in installs:
        parsed = typed_install(task)
        if parsed is None:
            return unknown({**detail, "missing": "typed install operation"}), None
        contradiction = _install_contradiction(task, *parsed)
        if contradiction:
            return failed({**detail, "contradiction": contradiction}), None
        typed[task["id"]] = parsed[1]
    if any(op.preflight is None or op.verification is None for op in typed.values()):
        return unknown({**detail, "missing": "preflight and verification"}), None
    if any(op.base_sha is None or op.head_sha is None for op in typed.values()):
        return unknown({**detail, "missing": "install base and head"}), None
    if pull_request is None or not pull_request.base_sha or not pull_request.head_sha:
        return unknown({**detail, "missing": "pull request base and head"}), None
    current = pull_request.base_sha
    while typed:
        starting = [key for key, op in typed.items() if op.base_sha == current]
        if len(starting) != 1:
            return failed(
                {**detail, "contradiction": "an install did not start on the scaffold"}
            ), None
        op = typed.pop(starting[0])
        commits = published.get(starting[0])
        if commits is None:
            return unknown({**detail, "missing": "the install's published commits"}), None
        if commits.status != "ahead" or not commits.commits:
            return failed({**detail, "contradiction": "the install published nothing"}), None
        current = op.head_sha
    if install_to_head is None:
        return unknown({**detail, "missing": "the story head against the install"}), None
    if install_to_head.status not in _CONTAINS:
        return failed({**detail, "contradiction": "the story head lacks the install"}), None
    return observed(detail), current


def glue_only(
    tasks: list[dict], plan: CapabilityPlan | None, engineering_delta: CompareFacts | None
) -> Finding:
    """What engineering changed after the install, against the glue the kit admitted.

    The admitted glue is the kit's own product-side conflict list from each install's
    preflight, by file. A `module` order admits nothing else; a `module_with_glue`
    requirement's glue has no file-level boundary the platform records, so a change
    outside the preflight's files is unknown there and failed everywhere else.
    """
    admitted: set[str] = set()
    for task in install_tasks(tasks):
        parsed = typed_install(task)
        if parsed is None or parsed[1].preflight is None:
            return unknown("the install's preflight is not readable")
        install, op = parsed
        admitted |= {
            item.path
            for item in op.preflight.outstanding_glue(install)
            if item.owner == PRODUCT_GLUE_OWNER and item.path
        }
    if engineering_delta is None:
        return unknown("the engineering change after the install is not readable")
    changed = sorted(engineering_delta.files)
    outside = [path for path in changed if path not in admitted]
    detail = {
        "base": engineering_delta.base,
        "head": engineering_delta.head,
        "changed_files": changed,
        "admitted_glue_files": sorted(admitted),
        "outside_admitted_glue": outside,
    }
    if not outside:
        return observed(detail)
    routes = {item.route for item in plan.capabilities} if plan is not None else set()
    if CapabilityRoute.MODULE_WITH_GLUE in routes:
        return unknown(detail)
    return failed(detail)


# --- CI, publication, deploy and QA --------------------------------------------------


def product_ci(story: dict) -> Finding:
    timeline = story.get("generated_product_timeline")
    if not isinstance(timeline, dict):
        return unknown("the story holds no generated-product timeline")
    pull_request = timeline.get("pull_request") or {}
    runs = [run for run in timeline.get("ci_runs") or [] if isinstance(run, dict)]
    detail = {
        "pull_request": {
            key: pull_request.get(key)
            for key in ("number", "state", "merged_at", "head_sha", "merge_commit_sha")
        },
        "ci_runs": [
            {key: run.get(key) for key in ("id", "url", "conclusion", "head_sha", "branch")}
            for run in runs
        ],
    }
    if not pull_request.get("merged_at"):
        return unknown(detail)
    green = [
        run
        for run in runs
        if run.get("conclusion") == "success"
        and run.get("head_sha") == pull_request.get("head_sha")
    ]
    return observed(detail) if green else unknown(detail)


def latest_terminal(runs: list[dict]) -> dict | None:
    terminal = [run for run in runs if run.get("status") in _TERMINAL_RUNS]
    return max(terminal, key=lambda run: run.get("completed_at") or "", default=None)


def deploy_typed(project_id: str, story_id: str, runs: list[dict]) -> tuple[Finding, dict | None]:
    """The story's terminal deploy, typed and correlated; its provenance is judged next."""
    run = latest_terminal(runs)
    if run is None:
        return unknown("the story has no terminal deploy run"), None
    result = typed_deploy_result(run)
    placed = (result.deployment_result if result is not None else None) or {}
    detail = {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "completed_at": run.get("completed_at"),
        "deploy_outcome": None if result is None else result.deploy_outcome.value,
        "deployed_url": None if result is None else result.deployed_url,
        "application_id": None if result is None else result.application_id,
        "bot_username": None if result is None else result.bot_username,
        # The deployer's own record of what it placed: the publication run that built
        # the images, the commit they are, the references the target pulled and the
        # digests those references resolved to.
        "publication_run_id": placed.get("run_id"),
        "deployed_commit_sha": placed.get("deployed_commit_sha"),
        "image_references": placed.get("image_references"),
        "image_digests": placed.get("image_digests"),
    }
    if str(run.get("project_id")) != project_id or run.get("story_id") != story_id:
        return failed({**detail, "correlation": "wrong project or story"}), None
    if (
        result is None
        or run.get("status") != RunStatus.COMPLETED.value
        or result.deploy_outcome is not DeployOutcome.SUCCESS
        or not result.deployed_url
        or not result.bot_username
    ):
        return failed(detail), None
    return observed(detail), detail


def deploy_provenance(  # noqa: PLR0911 - one chain, each link its own verdict
    deploy: dict,
    story: dict,
    pull_request: PullRequestFacts | None,
    publication: WorkflowRunFacts | None,
) -> Finding:
    """The chain PR head -> merged commit -> successful publication -> deployed digests."""
    timeline = story.get("generated_product_timeline") or {}
    recorded_head = (timeline.get("pull_request") or {}).get("head_sha")
    references = deploy.get("image_references") or {}
    digests = deploy.get("image_digests") or {}
    commit = deploy.get("deployed_commit_sha")
    chain = {
        **deploy,
        "pull_request": None
        if pull_request is None
        else {
            "number": pull_request.number,
            "head_sha": pull_request.head_sha,
            "merge_commit_sha": pull_request.merge_commit_sha,
        },
        "publication": None
        if publication is None
        else {
            "id": publication.id,
            "head_sha": publication.head_sha,
            "conclusion": publication.conclusion,
            "path": publication.path,
        },
    }
    if not deploy.get("publication_run_id") or not commit or not references:
        return unknown({**chain, "missing": "deployed commit, publication run and images"})
    if not isinstance(digests, dict) or set(digests) != set(references):
        return unknown({**chain, "missing": "a digest for every deployed image"})
    if not _SHA.fullmatch(str(commit)) or any(
        not _DIGEST.fullmatch(str(value)) for value in digests.values()
    ):
        return failed({**chain, "contradiction": "a commit or digest that is not one"})
    if pull_request is None or publication is None:
        return unknown({**chain, "missing": "the pull request and the publication run"})
    if recorded_head and pull_request.head_sha != recorded_head:
        return failed({**chain, "contradiction": "the merged head is not the recorded one"})
    if not pull_request.merged or not pull_request.merge_commit_sha:
        return unknown({**chain, "missing": "the merge commit"})
    if publication.head_sha != commit or publication.conclusion != "success":
        return failed({**chain, "contradiction": "the publication did not build this commit"})
    if commit != pull_request.merge_commit_sha:
        return failed({**chain, "contradiction": "the deployed commit is not the merge"})
    return observed(chain)


def qa_passed(project_id: str, story_id: str, runs: list[dict], deploy: dict | None) -> Finding:
    run = latest_terminal(runs)
    if run is None:
        return unknown("the story has no terminal QA run")
    result = typed_qa_result(run)
    detail = {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "completed_at": run.get("completed_at"),
        "qa_outcome": None if result is None else result.qa_outcome.value,
        "deployed_url": None if result is None else result.deployed_url,
        "passed_checks": [] if result is None else result.passed_checks[:50],
        "failed_checks": 0 if result is None else len(result.failed_checks),
        "unverified_checks": 0 if result is None else len(result.unverified_checks),
    }
    if str(run.get("project_id")) != project_id or run.get("story_id") != story_id:
        return failed({**detail, "correlation": "wrong project or story"})
    if result is None or result.qa_outcome is not QAOutcome.PASSED:
        return failed(detail)
    if deploy is None:
        return unknown({**detail, "correlation": "no successful deploy to bind to"})
    after = parse_time(run.get("completed_at"))
    deployed = parse_time(deploy.get("completed_at"))
    if result.deployed_url != deploy["deployed_url"] or after is None or deployed is None:
        return failed({**detail, "correlation": "not the story's deployed target"})
    if after < deployed:
        return failed({**detail, "correlation": "QA settled before the deploy"})
    return observed(detail)
