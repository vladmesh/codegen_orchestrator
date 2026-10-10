"""Third native catalog install on the same product, selected solely by capability."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import time

from level1_brief import Level1Brief
import mechanical_install as m
import pipeline_helpers as h

from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.stand_deadlines import (
    MECHANICAL_INSTALL_TIMEOUT,
    MECHANICAL_PLAN_TIMEOUT,
    MECHANICAL_QA_TIMEOUT,
    MECHANICAL_READBACK_TIMEOUT,
    SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT,
)

DATA = Path(__file__).resolve().parents[2] / "infra/stand-conversations/platform-module.json"


def enter(artifact, phase):
    artifact["phase"] = phase
    artifact.setdefault("phases", []).append({"phase": phase, "at": datetime.now(UTC).isoformat()})


def brief(marker):
    data = json.loads(DATA.read_text())
    return Level1Brief(
        marker=marker,
        title="Публичные каналы",
        summary="Читать публичные Telegram-каналы на русском и английском.",
        must_requirements=(
            {
                "id": "public_channels",
                "text": data["capability"],
                "user_wording": (
                    "Добавить публичный канал, читать дайджест и получать новые публикации "
                    "в чате на RU и EN."
                ),
            },
        ),
        usage_examples=(
            {
                "requirement_id": "public_channels",
                "user_sends": data["steps"][0]["text"],
                "product_answers": data["steps"][0]["expect"]["exact"],
            },
        ),
        limitations=("Only public channels; content is delivered without translation.",),
        settings_key=data["initial_setting"]["key"],
        settings_value=data["initial_setting"]["value"],
        settings_description="Explicit product language: ru or en.",
        story_title="Read public Telegram channels",
        story_description=data["capability"],
        language="ru",
    )


@contextmanager
def story_scope(ctx, artifact):
    engineering = ctx[h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY]
    previous = ctx["level1_extension"]
    ctx.pop("level1_extension")
    try:
        with h.second_story_scope(ctx):
            try:
                yield
            finally:
                m.retain_qa(ctx, artifact, "qa")
                artifact["partial_deploy"] = {
                    key: ctx[key]
                    for key in (
                        "deploy_run_id",
                        "deploy_merge_commit_sha",
                        "story_ci_runs",
                        "level1_merge_artifact",
                    )
                    if key in ctx
                }
    finally:
        ctx["level1_platform_extension"] = ctx["level1_extension"]
        ctx["level1_extension"] = previous
        ctx[h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY] = engineering


def check_readback(facts, baseline, task, operation, references):
    publication = facts["publication"]
    pr = publication["pull_request"]
    m.require(
        pr["merged"]
        and pr["head"]["sha"] == operation["head_sha"]
        and pr["merge_commit_sha"] == publication["merge_sha"]
        and publication["merge_base"] == operation["base_sha"],
        "publication",
        "third install PR/head/merge readback differs",
    )
    m.require(
        any(
            run["head_sha"] == publication["merge_sha"]
            and run["event"] == "push"
            and run["path"] == ".github/workflows/ci.yml"
            and run["conclusion"] == "success"
            and run["status"] == "completed"
            for run in publication["merge_ci"]
        ),
        "publication",
        "third install has no green merge CI",
    )
    m.require(
        publication["owned_hashes"] == baseline["publication"]["owned_hashes"],
        "notes_preservation",
        "third install changed owned notes",
    )
    for service, deployed in facts["deployment"].items():
        m.require(
            deployed["reference"] == references[f"{service.upper()}_IMAGE"]
            and deployed["registry_digest"]
            and deployed["digests"]
            and all("@sha256:" in digest for digest in deployed["digests"])
            and any(
                digest.endswith("@" + deployed["registry_digest"]) for digest in deployed["digests"]
            ),
            "readback",
            "third install running images differ from accepted merge",
        )
        before = baseline["deployment"][service]
        m.require(
            deployed["distributions"] == before["distributions"]
            and all(
                deployed["hashes"].get(path) == digest
                for path, digest in before["hashes"].items()
                if path != "codegen_kit/_active_packages.py"
            ),
            "readback",
            "third install changed previous source, binding or distributions",
        )
    backend = facts["deployment"]["backend"]["component"]
    m.require(
        backend["version"] == task["install"]["package"]["version"]
        and backend["active"]["name"] == task["install"]["package"]["name"]
        and backend["active"]["version"] == backend["version"]
        and backend["active"]["manifest_sha256"] == backend["manifest_sha256"]
        and facts["deployment"]["backend"]["core"] == baseline["deployment"]["backend"]["core"]
        and facts["deployment"]["tg_bot"]["component"]["binding_sha256"]
        == task["install"]["binding"]["sha256"],
        "readback",
        "third install activation/version/binding differs from admitted catalog closure",
    )


async def native_third_story(api, api_internal, api_observer, ctx, *, debug_prefix):  # noqa: PLR0915
    from test_full_pipeline import (  # noqa: PLC0415
        _level1_extension_owner_told,
        _require_level1_merge_artifact,
    )

    parent = ctx["mechanical_acceptance"]
    artifact = parent["platform_story"] = {"status": "running", "phase": "brief"}
    parent.update(status="running", phase="platform_story")
    enter(artifact, "brief")
    baseline = parent["readback"]
    with story_scope(ctx, artifact):
        ctx["level1_brief"] = brief(ctx["level1_marker"])
        await h.create_level1_confirmed_brief(api, ctx)
        requested = await api.get(f"/api/stories/{ctx['story_id']}")
        requested.raise_for_status()
        artifact.update(
            story_id=ctx["story_id"],
            project_id=ctx["project_id"],
            brief=ctx["brief_read"],
            request_created_at=requested.json()["created_at"],
            planning_attempt_id=ctx["level1_planning_attempt_id"],
        )
        artifact["accounting"] = [m.sql_snapshot(ctx)]
        ctx["level1_qa_criteria"] = (
            "- GET /health returns 200\n- Stand conversation: platform-module"
        )
        await h._write_level1_qa_criteria(api, ctx)
        enter(artifact, "invocation")
        result = h.docker_exec_python_module(
            "langgraph",
            "src.scripted_install_plan",
            [
                "--project",
                ctx["project_id"],
                "--story",
                ctx["story_id"],
                "--attempt",
                ctx["level1_planning_attempt_id"],
                "--capability",
                json.loads(DATA.read_text())["capability"],
                "--requirement",
                "public_channels",
            ],
            timeout=MECHANICAL_PLAN_TIMEOUT,
        )
        artifact["invocation"] = m.command_result(result, "scripted_install_result")
        m.require(
            artifact["invocation"]["coverage_outcome"] == "admitted",
            "admission",
            "third story not admitted",
        )
        response = await api.get(f"/api/tasks/?story_id={ctx['story_id']}")
        response.raise_for_status()
        tasks = response.json()
        m.require(len(tasks) == 1, "admission", "third story must have one install task")
        task = tasks[0]
        release = json.loads(DATA.read_text())["release"]
        artifact["fixture_release"] = release
        m.require(
            task["install"]["package"]["version"] == release["version"]
            and task["install"]["package"]["tag"] == release["tag"]
            and task["install"]["binding"]["sha256"] == release["binding_sha256"],
            "admission",
            "catalog selection differs from the fixture's released binding",
        )
        m.require(
            task["type"] == "install"
            and task["dispatch_admitted"]
            and task["planning_attempt_id"] == ctx["level1_planning_attempt_id"],
            "admission",
            "third story has wrong task/claim",
        )
        ctx["task_id"], ctx["task_ids"] = task["id"], [task["id"]]
        await h.verify_level1_plan_is_this_runs_alone(api, ctx, when="after_admission")
        artifact["admitted_install"] = task["install"]
        coverage = await api.get(f"/api/product-briefs/{ctx['brief_id']}/coverage")
        coverage.raise_for_status()
        artifact["coverage"] = coverage.json()
        enter(artifact, "install")
        deadline = time.monotonic() + MECHANICAL_INSTALL_TIMEOUT
        while time.monotonic() < deadline:
            artifact["accounting"].append(m.sql_snapshot(ctx))
            response = await api.get(f"/api/tasks/{task['id']}")
            response.raise_for_status()
            task = response.json()
            operation = task["install_operation"]
            if operation:
                operation.pop("token", None)
                artifact["operation"] = operation
                m.require(
                    operation["state"] not in {"refused", "recovery_required"},
                    "install",
                    operation.get("detail") or operation["state"],
                )
            if task["status"] == TaskStatus.DONE:
                break
            await asyncio.sleep(m.POLL_SECONDS)
        else:
            raise h.Level1PhaseFailed("install", "third install publication deadline exceeded")
        operation = artifact["operation"]
        m.require(
            operation["state"] == "published" and operation["verification"],
            "publication",
            "third install has no verified head",
        )
        m.require(
            operation["verification"]["binding_sha256"] == task["install"]["binding"]["sha256"]
            and operation["verification"]["tooling_commit"] == task["install"]["tooling_commit"],
            "publication",
            "third executor differs from admitted closure",
        )
        m.require(
            operation["base_sha"] == baseline["publication"]["merge_sha"],
            "publication",
            "third install is not based on reminders merge",
        )
        m.execution_readback(ctx, artifact, operation, task["install"])
        enter(artifact, "deploy")
        deployed = await h.wait_brief_deploy_run(api_internal, ctx, timeout=h.DEPLOY_RUN_TIMEOUT)
        m.require(deployed is not None, "deploy", ctx.get("deploy_run_error"))
        _require_level1_merge_artifact(ctx, phase="deploy", debug_prefix=debug_prefix)
        await h.record_story_ci_runs(api, ctx)
        deployed = await h.wait_deploy_outcome(
            api_internal, ctx, timeout=SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT
        )
        await h.wait_deploy(api, api_observer, ctx, timeout=h.DEPLOY_TIMEOUT)
        m.require(
            deployed is not None and ctx["deploy_outcome"] == DeployOutcome.SUCCESS.value,
            "deploy",
            "third deploy failed",
        )
        artifact["deployed_ready_at"] = datetime.now(UTC).isoformat()
        seconds = (
            datetime.fromisoformat(artifact["deployed_ready_at"])
            - datetime.fromisoformat(artifact["request_created_at"])
        ).total_seconds()
        artifact["request_to_deployed"] = {
            "seconds": seconds,
            "guideline_seconds": 600,
            "within_guideline": seconds <= 600,
        }
        m.require(h.record_deployed_image_tags(ctx), "deploy", ctx.get("deployed_image_error"))
        m.require(
            ctx["deployed_commit_sha"] == ctx["deploy_merge_commit_sha"]
            and ctx["main_head_probe"]["sha"] == ctx["deploy_merge_commit_sha"],
            "deploy",
            "third images differ from merged head",
        )
        response = await api.get(f"/api/stories/{ctx['story_id']}")
        response.raise_for_status()
        pr = response.json()["pr_number"]
        component = {
            **task["install"]["package"],
            "module": task["install"]["binding"]["resource"].split(":", 1)[0],
        }
        enter(artifact, "issuance")
        result = h.docker_exec_python_module(
            "langgraph",
            "shared.live_harness_platform",
            [
                "--project",
                ctx["project_id"],
                "--project-name",
                ctx["project_name"],
                "--server",
                ctx["server_handle"],
                "--owner",
                h.GITHUB_ORG,
                "--repo",
                ctx["repo_name"],
                "--story",
                ctx["story_id"],
                "--base",
                operation["base_sha"],
                "--head",
                operation["head_sha"],
                "--merge",
                ctx["deploy_merge_commit_sha"],
                "--pr",
                str(pr),
                "--component",
                json.dumps(component),
            ],
            timeout=MECHANICAL_READBACK_TIMEOUT,
        )
        artifact["readback"] = m.command_result(result, "platform_readback")
        check_readback(
            artifact["readback"], baseline, task, operation, ctx["deployed_image_references"]
        )
        artifact["issuance"] = artifact["readback"]["issuance"]
        m.require(
            artifact["readback"]["deployment"]["backend"]["component"]["manifest_sha256"]
            == release["manifest_sha256"],
            "readback",
            "installed manifest differs from the fixture's catalog release",
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
        enter(artifact, "telegram")
        ctx["qa_result"] = await h.run_non_llm_qa(
            api_internal,
            ctx["story_id"],
            timeout=MECHANICAL_QA_TIMEOUT,
            record=lambda run: h.record_qa_run(ctx, run),
        )
        artifact["telegram"] = m.qa_probe(ctx)
        m.require(
            artifact["telegram"]["languages"] == {"ru": True, "en": True},
            "telegram",
            "third story needs RU and EN results",
        )
        await h.record_qa_settlement_evidence(api_internal, ctx)
        m.require(
            ctx.get("qa_settlement_error") is None, "accounting", "third QA accounting failed"
        )
        m.require(
            await h.wait_story_completed(api_internal, ctx) is not None,
            "completion",
            "third story did not complete",
        )
        await _level1_extension_owner_told(api_internal, ctx, debug_prefix=debug_prefix)
        artifact["revoke"] = await m.revoke_readback(api_internal, ctx)
        artifact["accounting"].append(m.sql_snapshot(ctx))
        m.require(
            all(
                row["role"] == "engineering" for row in artifact["accounting"][-1]["project_ledger"]
            ),
            "accounting",
            "third QA created executor ledger",
        )
        artifact["zero_model"] = m.model_observation(ctx)
        enter(artifact, "completed")
        artifact.update(status="passed")
    parent.update(status="passed", phase="completed")
