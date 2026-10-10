"""Native second story and redacted acceptance artifact for mega-noop only."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time

from install_witness import WitnessRefused, witness_publication
from level1_brief import Level1Brief
import pipeline_helpers as h
from run_evidence import evidence_output_directory

from scripts import service_release
from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.queues import ENGINEERING_QUEUE, WORKER_COMMANDS
from shared.stand_deadlines import (
    MECHANICAL_INSTALL_TIMEOUT,
    MECHANICAL_PLAN_TIMEOUT,
    MECHANICAL_QA_TIMEOUT,
    MECHANICAL_READBACK_TIMEOUT,
    MECHANICAL_REVOKE_TIMEOUT,
    SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT,
)

POLL_SECONDS = 3
TIMEZONE = "Etc/UTC"


def require(condition, phase, detail):
    if not condition:
        raise h.Level1PhaseFailed(phase, detail)


def command_result(result, event):
    rows = []
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == event:
            require("result" in row, "invocation", f"{event} omitted its result")
            rows.append(row["result"])
    require(
        len(rows) == 1 and result.returncode == 0,
        "invocation",
        f"{event} exited {result.returncode}; expected one result, found {len(rows)}",
    )
    return rows[0]


def readback(ctx, base, head, *, story=None, pr=None):
    args = [
        "--project-name",
        ctx["project_name"],
        "--server",
        ctx["server_handle"],
        "--owner",
        h.GITHUB_ORG,
        "--repo",
        ctx["repo_name"],
        "--base",
        base,
        "--head",
        head,
    ]
    if story:
        args += ["--story", story]
    if "deploy_merge_commit_sha" in ctx:
        args += ["--merge", ctx["deploy_merge_commit_sha"]]
    if pr is not None:
        args += ["--pr", str(pr)]
    result = h.docker_exec_python_module(
        "langgraph",
        "shared.live_harness_mechanical_readback",
        args,
        timeout=MECHANICAL_READBACK_TIMEOUT,
    )
    return command_result(result, "mechanical_readback")


def sql_snapshot(ctx):
    """Durable scoped accounting, including terminal rows a queue ACK has removed."""
    story = ctx["story_id"].replace("'", "''")
    project = ctx["project_id"].replace("'", "''")
    result = h._psql(f"""SELECT json_build_object(
      'engineering_runs', (SELECT count(*) FROM runs
          WHERE story_id='{story}' AND type='engineering'),
      'engineering_ledger', (SELECT count(*) FROM engineering_attempt_ledger
          WHERE story_id='{story}' AND role='engineering'),
      'engineering_reservations', (SELECT count(*) FROM engineering_budget_reservations
          WHERE story_id='{story}'),
      'project_ledger', (SELECT coalesce(json_agg(json_build_object('run_id',run_id,'role',role,
          'model',model,'provider',provider,'total_tokens',total_tokens)), '[]'::json)
          FROM engineering_attempt_ledger WHERE project_id='{project}'),
      'project_runs', (SELECT coalesce(json_agg(json_build_object('id',id,'story_id',story_id,
          'type',type,'decision',metadata->'executor_decision','status',status)), '[]'::json)
          FROM runs WHERE project_id='{project}'),
      'stories', (SELECT coalesce(json_agg(json_build_object(
          'id',id,'planning',planning)), '[]'::json)
          FROM stories WHERE project_id='{project}'));
    """)
    require(result.returncode == 0, "accounting", "story-scoped SQL evidence could not be read")
    facts = json.loads(result.stdout.strip())
    dispatched = []
    for queue in (ENGINEERING_QUEUE, WORKER_COMMANDS):
        for entry_id, fields in h._redis_json("XRANGE", queue, "-", "+"):
            payload = json.loads(h._flat_redis_fields(fields)["data"])
            # Only engineering work or a worker create for this install Story is forbidden.
            if payload.get("story_id") == ctx["story_id"] or (
                (payload.get("config") or {}).get("ownership", {}).get("story_id")
                == ctx["story_id"]
            ):
                dispatched.append({"queue": queue, "id": entry_id})
    facts["dispatch"] = dispatched
    require(
        all(
            facts[key] == 0
            for key in ("engineering_runs", "engineering_ledger", "engineering_reservations")
        )
        and not dispatched,
        "accounting",
        "unexpected engineering for native install Story",
    )
    require(
        all(
            row["model"] is None and row["provider"] is None and row["total_tokens"] is None
            for row in facts["project_ledger"]
        ),
        "model_activity",
        "project ledger records model activity",
    )
    for run in facts["project_runs"]:
        if run["type"] == "engineering":
            decision = run["decision"]
            require(
                decision is not None and decision["agent_type"] == "noop",
                "model_activity",
                "unexpected engineering executor",
            )
    for story_row in facts["stories"]:
        planning = story_row["planning"]
        require(
            planning is None or (not planning["channels"] and not planning["channel_failures"]),
            "model_activity",
            "unexpected planning channel or attempted model call",
        )
    return facts


def workflow_source_sha():
    """Bootstrap rsync excludes .git; the workflow supplies its synced revision."""
    source = os.environ.get("STAND_SOURCE_SHA")
    require(
        source is not None and service_release.GIT_SHA.fullmatch(source) is not None,
        "service_provenance",
        "STAND_SOURCE_SHA must name the full 40-character workflow revision",
    )
    return source


def service_provenance():
    deadline = time.monotonic() + 30

    def remaining():
        seconds = deadline - time.monotonic()
        require(seconds > 0, "service_provenance", "immutable service readback exceeded 30 seconds")
        return seconds

    source = workflow_source_sha()
    record = Path("deployed-service-images.json")
    release = service_release.load_release(record)
    require(
        release is not None and release.git_sha == source,
        "service_provenance",
        "stand must run the published service release for its exact synced source",
    )
    compose = ["docker", "compose"]
    for path in (
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.stand.yml",
        os.environ["STAND_SERVICE_RELEASE_COMPOSE"],
    ):
        compose += ["-f", path]
    config = json.loads(
        subprocess.check_output(
            [*compose, "config", "--format", "json"], text=True, timeout=remaining()
        )
    )

    def docker(args):
        return subprocess.check_output(["docker", *args], text=True, timeout=remaining())

    problems = service_release.readback(
        compose_config=config, record=record, deploy_path=Path.cwd(), run_docker=docker
    )
    require(not problems, "service_provenance", "; ".join(problems))
    return json.loads(record.read_text())


def model_observation(ctx):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "logs",
            "--no-color",
            "--since",
            ctx["mechanical_started_at"],
            "langgraph",
            "architect",
            "qa-worker",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(result.returncode == 0, "model_activity", "model-runtime log observation failed")
    # A refused model attempt is also forbidden, even with empty stand credentials.
    counts = {
        event: result.stdout.count(event) for event in ("llm_channel_used", "llm_channel_failed")
    }
    require(not any(counts.values()), "model_activity", "unexpected model channel attempted a call")
    return {
        "since": ctx["mechanical_started_at"],
        "events": counts,
        "services": ["langgraph", "architect", "qa-worker"],
    }


def execution_readback(ctx, artifact, operation, install):
    """The operation's one native publication, retained in the artifact before it is witnessed."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "logs",
            "--no-color",
            "--since",
            ctx["mechanical_started_at"],
            "scaffolder",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(result.returncode == 0, "execution", "native publication logs could not be read")
    rows = []
    for line in result.stdout.splitlines():
        _, separator, payload = line.partition("|")
        try:
            row = json.loads(payload if separator else line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    try:
        return witness_publication(artifact, rows, operation, install)
    except WitnessRefused as refusal:
        raise h.Level1PhaseFailed("execution", str(refusal)) from refusal


def qa_probe(ctx):
    run = ctx["qa_run"]
    result = run["result"]
    require(result["qa_outcome"] == "passed", "telegram", "QA did not pass")
    probe = json.loads(result["report"])
    require(
        probe["status"] == "passed" and probe["phase"] == "completed",
        "telegram",
        "QA did not complete its fixed real-chat probe",
    )
    # The grant records the *story* commit its deploy Run targeted
    # (`run_metadata.head_sha`, services/scheduler/src/tasks/supervisor/deploy.py
    # `_deploy_run_head_sha`), not the built merge commit; temporary_access.py reads
    # the deployed commit separately. Comparing it with the merge commit failed
    # mega-noop 37445448651 on a passed probe whenever the PR merged with a merge commit.
    require(
        probe["grant"]["head_sha"] == ctx["deploy_head_sha"]
        and probe["grant"]["application_id"] == ctx["application_id"],
        "grant",
        "QA grant does not name the proven deployment head and target",
    )
    require(
        not result.get("probe_runs"),
        "model_activity",
        "fixed QA unexpectedly started an executor probe",
    )
    return probe


async def revoke_readback(api_internal, ctx):
    run_id = ctx["qa_run"]["id"]
    deadline = time.monotonic() + MECHANICAL_REVOKE_TIMEOUT
    while time.monotonic() < deadline:
        response = await api_internal.get(f"/api/temporary-access-grants/tempaccess-{run_id}")
        response.raise_for_status()
        grant = response.json()
        if grant["status"] == "revoked":
            return {
                key: grant[key]
                for key in (
                    "id",
                    "status",
                    "revoked_at",
                    "revoke_run_id",
                    "head_sha",
                    "target_application_id",
                    "external_id",
                    "channel",
                )
            }
        await asyncio.sleep(POLL_SECONDS)
    raise h.Level1PhaseFailed("revoke", "native grant owner did not prove revocation")


def retain_qa(ctx, artifact, key):
    run = ctx.get("qa_run")
    if run is None:
        return
    result = run.get("result") or {}
    report = result.get("report")
    try:
        parsed = json.loads(report) if report else None
    except ValueError:
        parsed = None
    artifact[key] = {
        "run_id": run["id"],
        "status": run["status"],
        "outcome": result.get("qa_outcome"),
        "probe": parsed,
    }


@contextmanager
def install_scope(ctx):
    engineering_ids = list(ctx.get(h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY, ctx["task_ids"]))
    try:
        with h.second_story_scope(ctx):
            try:
                yield
            finally:
                artifact = ctx["mechanical_acceptance"]
                retain_qa(ctx, artifact, "install_qa")
                artifact["partial_deploy"] = {
                    key: ctx[key]
                    for key in (
                        "deploy_run_id",
                        "deploy_merge_commit_sha",
                        "deployed_image_references",
                        "story_ci_runs",
                        "level1_merge_artifact",
                    )
                    if key in ctx
                }
    finally:
        # The generic two-story helper accumulates engineering Tasks. INSTALL is
        # recorded separately and has no engineering attempt to capture.
        ctx[h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY] = engineering_ids


def require_complete(ctx):
    require(
        ctx["mechanical_acceptance"]["status"] == "passed",
        "lifecycle",
        "native install and fixed QA did not complete",
    )
    require(
        ctx["mechanical_acceptance"]["platform_story"]["status"] == "passed",
        "lifecycle",
        "platform install and bilingual conversation did not complete",
    )
    for key in (
        "no_intervention_error",
        "engineering_settlement_error",
        "undeploy_request_error",
        "undeploy_run_error",
        "application_after_undeploy_error",
        "undeploy_residue_error",
    ):
        require(key in ctx and ctx[key] is None, "lifecycle", f"{key} is missing or failed")
    require(ctx["undeploy_run"]["status"] == "completed", "undeploy", "undeploy Run failed")
    require(
        ctx["undeploy_residue"]["port_allocation_absent"] is True,
        "undeploy",
        "port allocation survives undeploy",
    )


def check_readback(facts, *, installed, baseline=None, operation=None, expected_references=None):
    publication = facts["publication"]
    require(
        publication["merge_sha"]
        and any(
            run["head_sha"] == publication["merge_sha"]
            and run["event"] == "push"
            and run["path"] == ".github/workflows/ci.yml"
            and run["status"] == "completed"
            and run["conclusion"] == "success"
            for run in publication["merge_ci"]
        ),
        "publication",
        "no green generated CI for the actual deployed merge",
    )
    deployment = facts["deployment"]
    require(
        set(deployment) == {"backend", "tg_bot"}, "readback", "both deployed services are required"
    )
    for service, value in deployment.items():
        require(
            value["digests"] and all("@sha256:" in digest for digest in value["digests"]),
            "readback",
            f"immutable deployed digest missing for {service}",
        )
        require(
            value["registry_digest"]
            and any(digest.endswith("@" + value["registry_digest"]) for digest in value["digests"]),
            "readback",
            f"running {service} image differs from its published registry digest",
        )
        if expected_references is not None:
            require(
                value["reference"] == expected_references[f"{service.upper()}_IMAGE"],
                "readback",
                f"running {service} image reference differs from the accepted merge",
            )
        if not installed:
            require(
                all(version is None for version in value["distributions"].values()),
                "readback",
                f"component already installed in {service}",
            )
    from framework.spec.package_resolution import CORE_VERSION  # noqa: PLC0415

    require(
        deployment["backend"]["core"] == CORE_VERSION, "readback", "deployed core differs from pin"
    )
    require(
        deployment["backend"]["distributions"]["codegen-kit-reminders"]
        == ("0.5.0" if installed else None),
        "readback",
        "unexpected reminders version/presence",
    )
    require(
        deployment["tg_bot"]["distributions"]["codegen-kit-textparse"]
        == ("0.1.0" if installed else None),
        "readback",
        "unexpected textparse version/presence",
    )
    binding = deployment["tg_bot"]["hashes"].get("services/tg_bot/bindings/reminders.yaml")
    require(bool(binding) == installed, "readback", "unexpected default binding presence")
    for path, digest in facts["publication"]["owned_hashes"].items():
        service = path.split("/")[1]
        require(
            deployment[service]["hashes"].get(path) == digest,
            "readback",
            "deployed notes source differs from GitHub",
        )
    if installed:
        pull_request = publication["pull_request"]
        require(
            pull_request
            and pull_request["merged"] is True
            and pull_request["merge_commit_sha"] == publication["merge_sha"]
            and pull_request["head"]["sha"] == operation["head_sha"],
            "publication",
            "GitHub App did not prove the native install PR/head/merge",
        )
        require(
            facts["publication"]["owned_hashes"] == baseline["publication"]["owned_hashes"],
            "notes_preservation",
            "notes source/registration changed during native installation",
        )
        require(
            binding == operation["verification"]["binding_sha256"],
            "readback",
            "binding differs from released resource",
        )
        require(
            operation["verification"]["component_targets"]
            == {
                "reminders": "2748ffd05a982b9d193f4e43d47f6e4a6ff70a21",
                "textparse": "d68d997fe43a6067bfadf25bd76283141e1fc598",
            },
            "readback",
            "component source targets differ from immutable releases",
        )
        require(
            operation["base_sha"] == baseline["publication"]["head"],
            "publication",
            "install base is not fetched current main",
        )
        require(
            facts["publication"]["merge_base"] == operation["base_sha"],
            "publication",
            "install head is not based on accepted main",
        )
        for path, digest in baseline["publication"]["owned_hashes"].items():
            require(
                operation["verification"]["protected_sha256"].get(path) == digest,
                "notes_preservation",
                "executor did not protect original notes files",
            )


def install_brief(marker: str) -> Level1Brief:
    """The second story's confirmed brief: install the catalog reminders package."""
    return Level1Brief(
        marker=marker,
        title="Install reminders",
        summary="Add the catalog reminders package to my notes bot.",
        must_requirements=(
            {
                "id": "install_reminders",
                "text": "Install reminders with its recommended parser and default bot binding.",
                "user_wording": "Install reminders and preserve my notes.",
            },
        ),
        usage_examples=(
            {
                "requirement_id": "install_reminders",
                "user_sends": "/remind buy milk in 2 minutes",
                "product_answers": "Scheduled for a word-month instant: buy milk",
            },
        ),
        limitations=("English, one-time reminders, one product timezone.",),
        settings_key="timezone",
        settings_value=TIMEZONE,
        settings_description="Explicit product-wide IANA timezone.",
        story_title="Install catalog reminders",
        story_description=(
            "Install the explicit reminders catalog capability into this live notes bot."
        ),
        language="en",
    )


async def native_second_story(  # noqa: PLR0915 - native owners execute each acceptance phase
    api, api_internal, api_observer, ctx, *, debug_prefix
):
    from test_full_pipeline import (  # noqa: PLC0415
        _level1_extension_owner_told,
        _require_level1_merge_artifact,
    )

    artifact = ctx["mechanical_acceptance"]
    first_head = ctx["deploy_merge_commit_sha"]
    artifact["first_telegram"] = qa_probe(ctx)
    artifact["phase"] = "baseline"
    artifact["baseline"] = readback(ctx, first_head, first_head)
    check_readback(
        artifact["baseline"],
        installed=False,
        expected_references=ctx["deployed_image_references"],
    )
    artifact["first_revoke"] = await revoke_readback(api_internal, ctx)
    marker = ctx["level1_marker"]
    with install_scope(ctx):
        artifact["phase"] = "brief"
        ctx["level1_brief"] = install_brief(marker)
        await h.create_level1_confirmed_brief(api, ctx)
        requested = await api.get(f"/api/stories/{ctx['story_id']}")
        requested.raise_for_status()
        artifact.update(
            story_id=ctx["story_id"],
            project_id=ctx["project_id"],
            request_created_at=requested.json()["created_at"],
            brief=ctx["brief_read"],
            planning_attempt_id=ctx["level1_planning_attempt_id"],
        )
        artifact["accounting"] = [sql_snapshot(ctx)]
        ctx["level1_qa_criteria"] = (
            f"- GET /health returns 200\n- Stand mechanical reminders: {marker}"
        )
        await h._write_level1_qa_criteria(api, ctx)
        artifact["phase"] = "invocation"
        args = [
            "--project",
            ctx["project_id"],
            "--story",
            ctx["story_id"],
            "--package",
            "reminders",
            "--attempt",
            ctx["level1_planning_attempt_id"],
            "--requirement",
            "install_reminders",
        ]
        result = h.docker_exec_python_module(
            "langgraph", "src.scripted_install_plan", args, timeout=MECHANICAL_PLAN_TIMEOUT
        )
        artifact["invocation"] = command_result(result, "scripted_install_result")
        require(
            artifact["invocation"]["coverage_outcome"] == "admitted",
            "admission",
            "planner did not admit coverage",
        )
        tasks = await api.get(f"/api/tasks/?story_id={ctx['story_id']}")
        tasks.raise_for_status()
        task_rows = tasks.json()
        require(len(task_rows) == 1, "admission", "expected exactly one task")
        task = task_rows[0]
        if task["install_operation"]:
            task["install_operation"].pop("token", None)
        artifact["admitted_task"] = task
        require(
            task["type"] == "install"
            and task["dispatch_admitted"] is True
            and task["planning_attempt_id"] == ctx["level1_planning_attempt_id"],
            "admission",
            "wrong admitted task/claim",
        )
        require(
            task["install"]["package"]["version"] == "0.5.0"
            and [(item["name"], item["version"]) for item in task["install"]["libraries"]]
            == [("textparse", "0.1.0")]
            and task["install"]["binding"]["functions"] == ["textparse.when"],
            "admission",
            "task lacks the actual released catalog closure",
        )
        ctx["task_id"] = task["id"]
        ctx["task_ids"] = [task["id"]]
        await h.verify_level1_plan_is_this_runs_alone(api, ctx, when="after_admission")
        brief = await api.get(f"/api/product-briefs/{ctx['brief_id']}")
        brief.raise_for_status()
        coverage = await api.get(f"/api/product-briefs/{ctx['brief_id']}/coverage")
        coverage.raise_for_status()
        artifact.update(brief=brief.json(), coverage=coverage.json(), phase="install")
        deadline = time.monotonic() + MECHANICAL_INSTALL_TIMEOUT
        while time.monotonic() < deadline:
            artifact["accounting"].append(sql_snapshot(ctx))
            response = await api.get(f"/api/tasks/{task['id']}")
            response.raise_for_status()
            task = response.json()
            artifact["task"] = task
            operation = task["install_operation"]
            if operation:
                operation.pop("token", None)
                artifact["operation"] = operation
                require(
                    operation["state"] not in ("refused", "recovery_required"),
                    f"install_{operation['stage']}",
                    operation.get("detail") or operation["state"],
                )
            if task["status"] == TaskStatus.DONE:
                break
            await asyncio.sleep(POLL_SECONDS)
        else:
            raise h.Level1PhaseFailed(
                "install", "native install did not publish before bounded deadline"
            )
        operation = artifact["operation"]
        artifact["phase"] = "publication"
        require(
            operation["state"] == "published" and operation["verification"],
            "publication",
            "operation has no verified published head",
        )
        execution_readback(ctx, artifact, operation, task["install"])
        require(
            operation["verification"]["binding_sha256"] == task["install"]["binding"]["sha256"]
            and operation["verification"]["tooling_commit"] == task["install"]["tooling_commit"],
            "publication",
            "executor readback differs from the admitted closure",
        )
        artifact["publication"] = readback(
            ctx, operation["base_sha"], operation["head_sha"], story=ctx["story_id"]
        )["publication"]
        require(
            artifact["publication"]["branch_head"] == operation["head_sha"],
            "publication",
            "GitHub App remote branch readback differs from published install head",
        )
        artifact["phase"] = "deploy"
        # A parked install story (PR CI failure, merge refusal) never deploys; stop at
        # its park and keep the story's quarantine and PR/CI observations, which
        # run 37572801062 lost by waiting out the whole deploy budget instead.
        deploy_run = await h.wait_brief_deploy_run(api_internal, ctx, timeout=h.DEPLOY_RUN_TIMEOUT)
        artifact["story_observations"] = ctx.get("generated_product_story_observations", [])
        require(
            deploy_run is not None,
            "deploy",
            "install story produced no deploy Run: "
            + ctx.get("deploy_run_error", "no deploy run appeared"),
        )
        _require_level1_merge_artifact(ctx, phase="deploy", debug_prefix=debug_prefix)
        await h.record_story_ci_runs(api, ctx)
        deployed = await h.wait_deploy_outcome(
            api_internal, ctx, timeout=SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT
        )
        await h.wait_deploy(api, api_observer, ctx, timeout=h.DEPLOY_TIMEOUT)
        require(
            deployed is not None and ctx["deploy_outcome"] == DeployOutcome.SUCCESS.value,
            "deploy",
            "native deployment did not succeed",
        )
        artifact["deployed_ready_at"] = datetime.now(UTC).isoformat()
        duration = (
            datetime.fromisoformat(artifact["deployed_ready_at"])
            - datetime.fromisoformat(artifact["request_created_at"])
        ).total_seconds()
        artifact["request_to_deployed"] = {
            "seconds": duration,
            "guideline_seconds": 600,
            "within_guideline": duration <= 600,
        }
        require(h.record_deployed_image_tags(ctx), "deploy", ctx.get("deployed_image_error"))
        require(
            ctx["deployed_commit_sha"] == ctx["deploy_merge_commit_sha"]
            and ctx["main_head_probe"]["sha"] == ctx["deploy_merge_commit_sha"],
            "deploy",
            "deployment and image publication do not name the accepted merge head",
        )
        story_response = await api.get(f"/api/stories/{ctx['story_id']}")
        story_response.raise_for_status()
        pr_number = story_response.json()["pr_number"]
        require(pr_number is not None, "publication", "install story recorded no PR")
        artifact["phase"] = "readback"
        artifact["readback"] = readback(
            ctx, operation["base_sha"], operation["head_sha"], pr=pr_number
        )
        check_readback(
            artifact["readback"],
            installed=True,
            baseline=artifact["baseline"],
            operation=operation,
            expected_references=ctx["deployed_image_references"],
        )
        artifact["deploy"] = {
            key: ctx[key]
            for key in (
                "deploy_run_id",
                "deploy_run_record",
                "deploy_merge_commit_sha",
                "deployed_image_references",
                "story_ci_runs",
                "level1_merge_artifact",
            )
        }
        artifact["phase"] = "telegram"
        ctx["qa_result"] = await h.run_non_llm_qa(
            api_internal,
            ctx["story_id"],
            timeout=MECHANICAL_QA_TIMEOUT,
            record=lambda run: h.record_qa_run(ctx, run),
        )
        artifact["telegram"] = qa_probe(ctx)
        await h.record_qa_settlement_evidence(api_internal, ctx)
        require(
            ctx.get("qa_settlement_error") is None, "model_activity", "unexpected QA accounting"
        )
        require(
            await h.wait_story_completed(api_internal, ctx) is not None,
            "completion",
            "install story did not complete",
        )
        await _level1_extension_owner_told(api_internal, ctx, debug_prefix=debug_prefix)
        artifact["revoke"] = await revoke_readback(api_internal, ctx)
        artifact["accounting"].append(sql_snapshot(ctx))
        require(
            all(
                row["role"] == "engineering" for row in artifact["accounting"][-1]["project_ledger"]
            ),
            "model_activity",
            "deterministic QA created an executor ledger row",
        )
        artifact["zero_model"] = model_observation(ctx)
        artifact.update(status="passed", phase="completed")


def write_artifact(ctx):
    """Write partial facts on all exits; the workflow's suite verdict also owns cleanup."""
    artifact = ctx["mechanical_acceptance"]
    retain_qa(ctx, artifact, "first_qa")
    try:
        artifact["source_sha"] = workflow_source_sha()
    except h.Level1PhaseFailed as exc:
        artifact["source_sha"] = None
        artifact["source_sha_error"] = str(exc)
    artifact["kit"] = {
        "source": TEMPLATE_PIN.source,
        "ref": TEMPLATE_PIN.ref,
        "core": artifact["baseline"]["deployment"]["backend"]["core"]
        if "baseline" in artifact
        else None,
        "reminders": "0.5.0",
        "textparse": "0.1.0",
    }
    artifact["total_suite_seconds"] = time.monotonic() - ctx["mechanical_started"]
    artifact["bootstrap_included"] = False
    destination = evidence_output_directory() / f"mechanical-install-{ctx['manifest'].run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(h.redacted_payload(artifact), indent=2, default=str) + "\n")
    return destination
