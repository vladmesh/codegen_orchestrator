"""Pipeline test: Full deploy - THE MEGA TEST.

Exercises the entire path from project creation to a live /health response:

  1. API: create project + repo
  2. scaffold:queue → scaffolder → GitHub repo (+ branch protection on main)
  3. API: create story (in_progress) + task (todo)
  4. task_dispatcher → engineering:queue → worker → commit + push to story branch
  5. All tasks done → dispatcher creates PR story/{id} → main (auto-merge enabled)
  6. CI runs on PR → green → auto-merge → webhook → deploy:queue
  7. deploy consumer → DevOps subgraph → GitHub Actions deploy.yml
  8. smoke test: GET /health → 200

The noop path stays deterministic. The LLM path exercises the product route where a
real developer worker changes code before CI, merge, deploy, health, and QA.

The deterministic path is level-1: a Telegram-bot product with modules `backend`
and `tg_bot`, its bot token bound through the product route, and an engineering
task per change set that the merged scripted runner applies. What it deploys
therefore carries a backend endpoint, a product-scoped setting and a Telegram
command handler that no scaffolded tree has — and the assertions below ask the
deployment for them, not the repository.
"""

import os

from level1_brief import (
    LEVEL1_BRIEF_LANGUAGE,
    bot_completion_message_mismatches,
    level1_extension_settings_value,
    level1_settings_value,
)
from level1_change_set import (
    LEVEL1_EXTENSION_ENDPOINT_PATH,
    LEVEL1_EXTENSION_SETTING_KEY,
    LEVEL1_SETTING_KEY,
)
from level1_second_story import (
    DEPLOY_PATH_OWNER_GRANT,
    DEPLOY_PATH_PR_POLLER,
    branch_base_mismatches,
    checkout_mismatches,
    ci_run_mismatches,
    deploy_path_mismatches,
    engineering_run_mismatches,
    product_hook_mismatches,
    workspace_reuse_mismatches,
)
from live_harness import cleanup_guard
from pipeline_helpers import (
    DEPLOY_OUTCOME_TIMEOUT,
    DEPLOY_RUN_TIMEOUT,
    DEPLOY_TIMEOUT,
    ENGINEERING_TIMEOUT,
    EXPECTED_ENV_CONTRACT_FRAGMENTS,
    LLM_ENGINEERING_TIMEOUT,
    SECOND_STORY_CHECKOUT_BOUND_SECONDS,
    Level1PhaseFailed,
    ScaffoldDidNotComplete,
    admit_level1_extension_plan,
    admit_level1_plan,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    begin_level1_extension_story,
    cleanup_all,
    configured_qa_executor,
    create_level1_bot_project,
    create_level1_confirmed_brief,
    create_level1_extension_brief,
    create_llm_backend_project,
    create_story_and_task,
    dump_debug,
    ensure_test_user,
    evidence_pass,
    level1_completion_text_requirement,
    po_input_cursor,
    read_product_setting,
    record_deployed_image_tags,
    record_engineering_failure_steps,
    record_env_contract,
    record_first_checkout,
    record_health_probe,
    record_level1_extension_product_evidence,
    record_level1_product_evidence,
    record_level1_scripted_path,
    record_manager_checkout_script,
    record_noop_settlement_evidence,
    record_qa_run,
    record_settings_seed_brief_log,
    record_story_branch_ahead,
    record_story_branch_base,
    record_story_ci_runs,
    record_story_engineering_runs,
    record_terminal_stage_evidence,
    request_undeploy,
    run_non_llm_qa,
    second_story_scope,
    trigger_scaffold,
    verify_level1_plan_is_this_runs_alone,
    verify_linear_noop_story_completion,
    verify_undeploy_residue,
    wait_application_not_deployed,
    wait_deploy,
    wait_deploy_outcome,
    wait_deploy_run,
    wait_engineering,
    wait_linear_noop_engineering,
    wait_owner_completion_notification,
    wait_scaffold,
    wait_service_deployment,
    wait_story_completed,
    wait_undeploy_run,
)
import pytest
import pytest_asyncio
import run_evidence
from run_evidence import CaptureStatus, RunEvidenceCollector, emit_run_evidence

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.telegram import TokenCheckName
from shared.contracts.queues.deploy import DeployOutcome
from shared.stand_deadlines import QA_RUN_TIMEOUT, SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _pipeline_run(
    create_project,
    *,
    engineering_timeout: int,
    debug_prefix: str,
    lifecycle_undeploy: bool = False,
    require_story_commit: bool = False,
):
    """Full pipeline: scaffold → engineering → deploy. Yields context for assertions."""
    async with api_client_as_test_user() as api:
        # Deploy runs belong to no user, and list_runs hides unowned runs from the
        # non-admin harness user, so they are observed through a client that
        # authenticates only as an internal service and names no user.
        async with (
            api_client_as_unscoped_observer() as api_observer,
            api_client_as_internal_service() as api_internal,
        ):
            # The fixture user is registered by the service, then touched as
            # itself: registration is promo-gated for a named actor.
            await ensure_test_user(api, api_internal)
            ctx = await create_project(api, api_internal)
            async with cleanup_guard(
                lambda: cleanup_all(api_internal, api_observer, ctx), manifest=ctx["manifest"]
            ):
                # One artifact per combination, written before teardown removes
                # the containers it is collected from. The collector needs one
                # fact: this run's id — the same identity the project was
                # created with, which every worker this run causes carries as
                # `com.codegen.run.id` from the moment it exists.
                ctx["qa_agent_type_requested"] = os.getenv("LIVE_QA_AGENT_TYPE")
                ctx["run_evidence"] = RunEvidenceCollector(
                    run_id=ctx["manifest"].run_id,
                    # The second source, for the one case a label query cannot
                    # answer: a container that was removed rather than killed.
                    owned_workers=lambda: [
                        resource.identifier
                        for resource in ctx["manifest"].resources
                        if resource.kind == "worker"
                    ],
                )
                try:
                    async for value in _pipeline_phases(
                        api,
                        api_internal,
                        api_observer,
                        ctx,
                        engineering_timeout=engineering_timeout,
                        debug_prefix=debug_prefix,
                        lifecycle_undeploy=lifecycle_undeploy,
                        require_story_commit=require_story_commit,
                    ):
                        yield value
                finally:
                    # Always ahead of cleanup_all, which is what removes the
                    # containers — and removal, not death, is what ends the
                    # readability of a labelled worker. The same deadline holds
                    # for the deployment on the target host and for the QA Run a
                    # phase that raised never looked for, so both are read here.
                    await record_terminal_stage_evidence(api_internal, ctx)
                    evidence_pass(ctx)
                    emit_run_evidence(ctx)


async def _complete_level1_story(api_internal, ctx: dict, *, debug_prefix: str) -> bool:
    """Take one level-1 story to its own ending: QA, completion, notification.

    Split from the undeploy below because the lifecycle now runs *two* stories
    on one project and only the second one is followed by the teardown. The
    first story has to reach its ending — and be seen to — before the extension
    story may be created at all: `create_story` queues a story behind an active
    one instead of publishing it, and the PR poller only takes the ordinary
    deploy path for a project that already has a completed story.
    """
    if ctx.get("qa_result", {}).get("qa_outcome") != "passed":
        dump_debug(ctx, f"{debug_prefix}-qa")
        return False
    if await wait_story_completed(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-story-completed")
        return False
    if (
        await wait_owner_completion_notification(
            api_internal, ctx, text_requirement=level1_completion_text_requirement(ctx)
        )
        is None
    ):
        dump_debug(ctx, f"{debug_prefix}-owner-notification")
        return False
    if await wait_service_deployment(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-service-deployment")
        return False
    return True


async def _undeploy_level1_product(api, api_internal, ctx: dict, *, debug_prefix: str) -> bool:
    """End the lifecycle: the product the two stories built is undeployed."""
    response = await api.get(f"/api/applications/{ctx['application_id']}")
    response.raise_for_status()
    ctx["application_before_undeploy"] = response.json()
    if ctx["application_before_undeploy"].get("status") != ApplicationStatus.RUNNING.value:
        ctx["application_before_undeploy_error"] = (
            f"application {ctx['application_id']} was not running before undeploy: "
            f"{ctx['application_before_undeploy'].get('status')}"
        )
        dump_debug(ctx, f"{debug_prefix}-pre-undeploy")
        return False
    await request_undeploy(api, api_internal, ctx)
    if ctx.get("undeploy_request_error"):
        dump_debug(ctx, f"{debug_prefix}-undeploy-request")
        return False
    if await wait_undeploy_run(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-undeploy-run")
        return False
    if await wait_application_not_deployed(api, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-not-deployed")
        return False
    if await verify_undeploy_residue(api_internal, ctx) is None:
        dump_debug(ctx, f"{debug_prefix}-undeploy-residue")
        return False
    return True


async def _level1_brief_plan_and_engineering(
    api,
    api_internal,
    ctx: dict,
    *,
    engineering_timeout: int,
    debug_prefix: str,
) -> None:
    """The level-1 story: a confirmed brief, an admitted plan, two scripted tasks.

    Every exit from here that is not "the story is built" raises naming its own
    phase, the way card 1310 made the scaffold raise. The level-1 route is
    deterministic — nothing in it is allowed to fail for a reason that is not
    this platform's — so a phase that did not produce what the phases after it
    are about is a failure of that phase, never a skip and never an assertion
    about a product that was never built.
    """
    # A cursor fences out historical and foreign PO events. It is captured
    # before the story can produce a completion notification.
    ctx["po_input_cursor"] = po_input_cursor()
    try:
        await create_level1_confirmed_brief(api, ctx)
        await admit_level1_plan(api, ctx)
    except Level1PhaseFailed as failure:
        dump_debug(ctx, f"{debug_prefix}-{failure.phase}")
        raise

    await wait_linear_noop_engineering(
        api, api_internal, ctx, timeout=engineering_timeout, on_poll=lambda: evidence_pass(ctx)
    )
    if ctx.get("task_status") != TaskStatus.DONE:
        # A failed scripted step has a name; the run says which one before it
        # stops, so its evidence reads "setup" rather than only "engineering".
        record_engineering_failure_steps(ctx)
        dump_debug(ctx, f"{debug_prefix}-engineering")
        raise Level1PhaseFailed(
            "engineering",
            f"task status {ctx.get('task_status')}; "
            f"failed steps {ctx.get('engineering_failure_steps')}",
        )
    await record_noop_settlement_evidence(api_internal, ctx)
    if ctx.get("noop_settlement_error") is not None:
        dump_debug(ctx, f"{debug_prefix}-noop-settlement")
        raise Level1PhaseFailed("engineering", ctx["noop_settlement_error"])
    if not await verify_linear_noop_story_completion(api, ctx):
        dump_debug(ctx, f"{debug_prefix}-noop-linear-story")
        raise Level1PhaseFailed("engineering", ctx["linear_noop_completion_error"])
    # Last, because an architect that won the claim would have spent minutes on
    # its model turn and landed its own tasks in this story well after the
    # admission: the roster is asked again once engineering is settled.
    try:
        await verify_level1_plan_is_this_runs_alone(api, ctx, when="after_engineering")
    except Level1PhaseFailed as failure:
        dump_debug(ctx, f"{debug_prefix}-{failure.phase}")
        raise


async def _record_level1_settings_seed_evidence(ctx: dict, deploy_result, *, key: str) -> None:
    """What the deploy did with the confirmed brief's initial settings.

    Three reads, all while the deployment is still up: the deploy run's own
    per-setting record, the deploy consumer's statement of which brief it read
    and by which route, and the value the *product* itself now holds.

    `key` is the setting the story's own brief confirmed — the first story's, or
    the extension story's second key — so the readback is of what *this* story
    seeded and cannot be answered by what the previous one did.
    """
    ctx["level1_settings_seed"] = [
        seed.model_dump(mode="json") for seed in deploy_result.settings_seed
    ]
    record_settings_seed_brief_log(ctx)
    ctx["level1_settings_readback"] = await read_product_setting(ctx, key=key)


async def _level1_lifecycle_tail(
    api, api_internal, api_observer, ctx: dict, *, debug_prefix: str
) -> bool:
    """The rest of the level-1 lifecycle once the first story has deployed.

    Three phases, in an order that is not a preference. The first story has to
    reach its own ending before the extension story is created at all — a story
    created while another is in progress is queued rather than published, and
    the deploy path the extension story must take is the one a project with a
    completed story gets. The undeploy comes last, because the product it
    removes is the one *both* stories built.
    """
    if not await _complete_level1_story(api_internal, ctx, debug_prefix=debug_prefix):
        return False
    await _level1_extension_story(api, api_internal, api_observer, ctx, debug_prefix=debug_prefix)
    return await _undeploy_level1_product(api, api_internal, ctx, debug_prefix=debug_prefix)


async def _level1_extension_story(
    api, api_internal, api_observer, ctx: dict, *, debug_prefix: str
) -> None:
    """The second story of the same project, on the workspace the first left behind.

    This is the part of the level-1 scenario sprint:1445 exists for: five of the
    seven regressions it found by hand lived *only* here, and not one of them is
    visible in the story's success. A second story can deploy, pass QA and
    complete while its first checkout was retried past a dead worker, while its
    branch was cut from a stale workspace HEAD, or while its deploy went through
    the initial-owner grant that belongs to a project's first story alone.

    So the observations that can only be made here are taken as the story runs —
    the manager's own checkout pair, the script the running manager built, the
    Run rows of every engineering attempt, where the branch forked and what that
    commit contains, which path minted the deploy Run — and every one of them is
    recorded before the phase that could raise over it. Judging them is the
    tests' job; this function's job is that a green run has them.

    Every exit that is not "the extension story is built, deployed and accepted"
    raises `Level1PhaseFailed` naming its own phase, the same way the first
    story's phases do. Nothing here may be skipped: a phase that did not produce
    what the phases after it are about is a failure of that phase.
    """
    # The commit the first story merged. The extension branch has to be cut from
    # a default branch that contains it, and this is the last moment the first
    # story's deploy facts are still the current ones.
    first_story_merge_commit = ctx.get("deploy_merge_commit_sha")
    with second_story_scope(ctx):
        begin_level1_extension_story(ctx)
        try:
            await create_level1_extension_brief(api, ctx)
            await admit_level1_extension_plan(api, ctx)
        except Level1PhaseFailed as failure:
            dump_debug(ctx, f"{debug_prefix}-extension-{failure.phase}")
            raise

        await wait_engineering(
            api, ctx, timeout=ENGINEERING_TIMEOUT, on_poll=lambda: evidence_pass(ctx)
        )
        # Read before anything is judged. A failed engineering phase is exactly
        # when the checkout evidence is worth having, and the manager's log is
        # a tail: waiting until after a raise would be waiting until it is gone.
        record_first_checkout(ctx)
        record_manager_checkout_script(ctx)
        await record_story_engineering_runs(api_internal, ctx)
        if ctx.get("task_status") != TaskStatus.DONE:
            record_engineering_failure_steps(ctx)
            dump_debug(ctx, f"{debug_prefix}-extension-engineering")
            raise Level1PhaseFailed(
                "extension_engineering",
                f"task status {ctx.get('task_status')}; "
                f"failed steps {ctx.get('engineering_failure_steps')}",
            )
        await record_noop_settlement_evidence(api_internal, ctx)
        if ctx.get("noop_settlement_error") is not None:
            dump_debug(ctx, f"{debug_prefix}-extension-noop-settlement")
            raise Level1PhaseFailed("extension_engineering", ctx["noop_settlement_error"])
        try:
            await verify_level1_plan_is_this_runs_alone(api, ctx, when="after_engineering")
        except Level1PhaseFailed as failure:
            dump_debug(ctx, f"{debug_prefix}-extension-{failure.phase}")
            raise
        record_level1_scripted_path(ctx)
        record_story_branch_base(ctx, contains_sha=first_story_merge_commit or "")

        deploy_run = await wait_deploy_run(api_internal, ctx, timeout=DEPLOY_RUN_TIMEOUT)
        if deploy_run is None:
            dump_debug(ctx, f"{debug_prefix}-extension-deploy-run")
            raise Level1PhaseFailed(
                "extension_deploy", ctx.get("deploy_run_error", "no deploy run appeared")
            )
        # The scheduler writes the generated product's CI observations onto the
        # story while it waits for the merge commit's images, so they exist by
        # the time a deploy Run does.
        await record_story_ci_runs(api, ctx)
        if not record_env_contract(
            ctx, ctx["deploy_head_sha"], phase="merged", verify_merged_into_main=True
        ):
            dump_debug(ctx, f"{debug_prefix}-extension-env-contract-merged")
            raise Level1PhaseFailed(
                "extension_deploy", (ctx.get("env_contract_errors") or {})["merged"]
            )

        deploy_result = await wait_deploy_outcome(
            api_internal, ctx, timeout=SECOND_STORY_DEPLOY_OUTCOME_TIMEOUT
        )
        # Only now is the application worth reading: it was `running` from the
        # first story's deploy throughout this one, so its status becomes an
        # answer about *this* deploy once this deploy's Run is terminal.
        await wait_deploy(api, api_observer, ctx, timeout=DEPLOY_TIMEOUT)
        if (
            deploy_result is None
            or ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
            or ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
        ):
            dump_debug(ctx, f"{debug_prefix}-extension-deploy")
            raise Level1PhaseFailed(
                "extension_deploy",
                f"deploy run {ctx.get('deploy_run_id')} ended "
                f"deploy_outcome={ctx.get('deploy_outcome')} "
                f"application={ctx.get('final_app_status')} "
                f"({ctx.get('deploy_error_details') or ctx.get('deploy_outcome_error')})",
            )

        if not ctx.get("deployed_url"):
            dump_debug(ctx, f"{debug_prefix}-extension-deployment-address")
            raise Level1PhaseFailed(
                "extension_deploy",
                f"application {ctx.get('application_id')} is "
                f"{ctx.get('final_app_status')} but this run resolved no address for it, "
                "so nothing after this could ask the deployment anything",
            )
        try:
            await record_health_probe(ctx, ctx["deployed_url"])
        except AssertionError as error:
            dump_debug(ctx, f"{debug_prefix}-extension-health")
            raise Level1PhaseFailed("extension_deploy", str(error)) from error
        await record_level1_extension_product_evidence(ctx)
        await _record_level1_settings_seed_evidence(
            ctx, deploy_result, key=LEVEL1_EXTENSION_SETTING_KEY
        )
        if not record_deployed_image_tags(ctx):
            dump_debug(ctx, f"{debug_prefix}-extension-deployed-images")
            raise Level1PhaseFailed("extension_deploy", ctx["deployed_image_error"])

        ctx["qa_result"] = await run_non_llm_qa(
            api_internal,
            ctx["story_id"],
            timeout=QA_RUN_TIMEOUT,
            record=lambda run: record_qa_run(ctx, run),
            on_poll=lambda: evidence_pass(ctx),
        )
        if ctx.get("qa_result", {}).get("qa_outcome") != "passed":
            dump_debug(ctx, f"{debug_prefix}-extension-qa")
            raise Level1PhaseFailed("extension_qa", f"QA ended {ctx.get('qa_result')}")

        if await wait_story_completed(api_internal, ctx) is None:
            dump_debug(ctx, f"{debug_prefix}-extension-story-completed")
            raise Level1PhaseFailed("extension_completion", ctx["story_terminal_error"])
        if (
            await wait_owner_completion_notification(
                api_internal, ctx, text_requirement=level1_completion_text_requirement(ctx)
            )
            is None
        ):
            dump_debug(ctx, f"{debug_prefix}-extension-owner-notification")
            raise Level1PhaseFailed("extension_completion", ctx["owner_notification_error"])


async def _pipeline_phases(
    api,
    api_internal,
    api_observer,
    ctx: dict,
    *,
    engineering_timeout: int,
    debug_prefix: str,
    lifecycle_undeploy: bool,
    require_story_commit: bool = False,
):
    """The pipeline phases themselves, so evidence can wrap every exit from them."""
    if ctx.get("qa_requires_executor"):
        ctx["qa_agent_type"] = configured_qa_executor()

    # Phase 1: Scaffold. A scaffold that does not reach `active` raises here,
    # naming its own phase, so it cannot be reported to the test session as an
    # assertion about engineering or the scripted path. The dump is written
    # first, because it is the post-mortem this failure is worth reading with.
    trigger_scaffold(ctx)
    try:
        await wait_scaffold(api, ctx)
    except ScaffoldDidNotComplete:
        dump_debug(ctx, f"{debug_prefix}-scaffold")
        raise

    # Phase 2: Engineering. Every poll takes an evidence pass: a retry removes
    # the previous attempt's container, and the attempt that died is exactly the
    # one that has to stay attributable.
    if lifecycle_undeploy:
        await _level1_brief_plan_and_engineering(
            api,
            api_internal,
            ctx,
            engineering_timeout=engineering_timeout,
            debug_prefix=debug_prefix,
        )
    else:
        await create_story_and_task(api, ctx)
        await wait_engineering(
            api, ctx, timeout=engineering_timeout, on_poll=lambda: evidence_pass(ctx)
        )
        if ctx.get("task_status") != TaskStatus.DONE:
            # A failed scripted step has a name; the run says which one before
            # it stops, so its evidence reads "setup" rather than "engineering".
            record_engineering_failure_steps(ctx)
            yield ctx
            dump_debug(ctx, f"{debug_prefix}-engineering")
            return

    # Both engineering tasks are settled, so what the story branch carries is
    # settled too: this is where the scripted path is told apart from the
    # runner's empty-commit fallback, one GitHub comparison, before any wait.
    if ctx.get("level1_change_set_paths"):
        record_level1_scripted_path(ctx)

    # The deploy that follows exists only if engineering committed something.
    # Task-done is the first moment that is settled and this is the last one
    # before the suite starts waiting on it, so the branch is compared with main
    # here — one GitHub call — instead of being inferred from a 420-second wait
    # for a deploy run that an unopenable story PR can never produce. Only the
    # paid path asks a real developer for a change, so only it can end with an
    # empty story branch; the deterministic route always commits, and its
    # phases, waits and evidence stay exactly as they were.
    if require_story_commit and not record_story_branch_ahead(ctx):
        yield ctx
        dump_debug(ctx, f"{debug_prefix}-story-branch")
        return

    # Phase 3: Deploy. The story branch merges into main and only then
    # does a deploy run appear carrying the merged head SHA. The ref
    # deploy reads the contract at. Re-check the contract there: the
    # scaffolded tree proves nothing about what engineering merged.
    deploy_run = await wait_deploy_run(api_internal, ctx, timeout=DEPLOY_RUN_TIMEOUT)
    if deploy_run is None:
        dump_debug(ctx, f"{debug_prefix}-deploy-run")
        if lifecycle_undeploy:
            raise Level1PhaseFailed("deploy", ctx.get("deploy_run_error", "no deploy run appeared"))
        yield ctx
        return
    if not record_env_contract(
        ctx,
        ctx["deploy_head_sha"],
        phase="merged",
        verify_merged_into_main=True,
    ):
        yield ctx
        dump_debug(ctx, f"{debug_prefix}-env-contract-merged")
        return

    await wait_deploy(api, api_observer, ctx, timeout=DEPLOY_TIMEOUT)
    deploy_result = await wait_deploy_outcome(api_internal, ctx, timeout=DEPLOY_OUTCOME_TIMEOUT)
    deploy_succeeded = (
        ctx.get("final_app_status") == ApplicationStatus.RUNNING.value
        and ctx.get("deploy_outcome") == DeployOutcome.SUCCESS.value
    )
    if lifecycle_undeploy and not deploy_succeeded:
        # The level-1 deploy is the one that carries the confirmed brief's
        # settings into the product; nothing after it is about anything else.
        dump_debug(ctx, f"{debug_prefix}-deploy")
        raise Level1PhaseFailed(
            "deploy",
            f"deploy run {ctx.get('deploy_run_id')} ended "
            f"deploy_outcome={ctx.get('deploy_outcome')} "
            f"application={ctx.get('final_app_status')} "
            f"({ctx.get('deploy_error_details') or ctx.get('deploy_outcome_error')})",
        )
    if deploy_succeeded:
        # The external probe happens while the application is running, but its
        # evidence remains available after the noop lifecycle undeploys it.
        # The probe keeps its raise; what it also does now is leave the
        # failure behind it, so an orchestrator that could not reach the
        # deployment is a stated read rather than an absent one.
        await record_health_probe(ctx, ctx["deployed_url"], expect_marker=ctx.get("health_marker"))
        # The level-1 product facts are read from the running deployment, here,
        # because the noop lifecycle undeploys it a few phases later.
        if ctx.get("level1_change_set_paths"):
            await record_level1_product_evidence(ctx)
        if lifecycle_undeploy:
            await _record_level1_settings_seed_evidence(ctx, deploy_result, key=LEVEL1_SETTING_KEY)
        # Before any QA attempt: the deployed images must be this commit's. A
        # successful deploy Run and an HTTP 200 are both compatible with the
        # host running an older image, and QA is where that shows up — as a
        # product failure, several steps and one paid executor turn too late.
        if record_deployed_image_tags(ctx):
            # The QA run is recorded before it is judged, so a QA cell can say
            # "exercised and failed" instead of falling back to "not exercised".
            # Every poll takes an evidence pass too: the QA executor's container is
            # removed as soon as the executor call returns, so this wait is the
            # window its exit code and log tail are still readable in.
            ctx["qa_result"] = await run_non_llm_qa(
                api_internal,
                ctx["story_id"],
                timeout=QA_RUN_TIMEOUT,
                record=lambda run: record_qa_run(ctx, run),
                on_poll=lambda: evidence_pass(ctx),
            )
        else:
            dump_debug(ctx, f"{debug_prefix}-deployed-images")

    if lifecycle_undeploy and not await _level1_lifecycle_tail(
        api, api_internal, api_observer, ctx, debug_prefix=debug_prefix
    ):
        yield ctx
        return

    yield ctx

    if (
        ctx.get("final_app_status") != ApplicationStatus.RUNNING.value
        or ctx.get("deploy_outcome") != DeployOutcome.SUCCESS.value
    ):
        dump_debug(ctx, f"{debug_prefix}-deploy")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def pipeline():
    """The level-1 Telegram-bot product: scaffold → scripted developer → deploy."""
    async for ctx in _pipeline_run(
        create_level1_bot_project,
        engineering_timeout=ENGINEERING_TIMEOUT,
        debug_prefix="full-level1",
        lifecycle_undeploy=True,
    ):
        yield ctx


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def llm_pipeline():
    """Full LLM pipeline: scaffold → real worker → deploy."""
    async for ctx in _pipeline_run(
        create_llm_backend_project,
        engineering_timeout=LLM_ENGINEERING_TIMEOUT,
        debug_prefix="full-llm",
        require_story_commit=True,
    ):
        yield ctx


def _no_probe(pipeline: dict, name: str) -> str:
    """Why a deployed-product probe is missing — a failure, never a skip.

    The probes run while the deployment is up. If the run never got that far the
    product assertions cannot pass, and saying which phase ended the run is more
    useful than a KeyError and far more useful than a skip that would report a
    failed deploy as an untested product.
    """
    return (
        f"{name} was never recorded: engineering ended {pipeline.get('task_status')}, "
        f"deploy_outcome={pipeline.get('deploy_outcome')}, "
        f"application={pipeline.get('final_app_status')}"
    )


def _extension(pipeline: dict) -> dict:
    """The second story's own facts, or why this run has none.

    Every failure inside the second story raises out of the fixture naming its
    own phase, so an assertion reached with no extension facts at all means the
    lifecycle stopped before the second story began. Saying that is more useful
    than a KeyError, and far more useful than a skip that would report a run
    which never ran a second story as one whose second story was fine.
    """
    extension = pipeline.get("level1_extension")
    assert extension, (
        "this run recorded no second story: the first story's engineering ended "
        f"{pipeline.get('task_status')}, deploy_outcome={pipeline.get('deploy_outcome')}, "
        f"application={pipeline.get('final_app_status')}"
    )
    return extension


class TestFullPipeline:
    """THE MEGA TEST: level-1 bot product → scripted developer → CI → deploy → QA."""

    async def test_the_product_is_a_two_module_bot_with_its_token_bound(self, pipeline):
        """Modules and binding are one decision: `tg_bot` is why the token is required."""
        assert pipeline["modules"] == ["backend", "tg_bot"]
        binding = pipeline.get("bot_binding")
        assert binding and binding["status"] == "ok", binding
        assert binding["bot_username"], binding
        # The route runs the whole chain server-side, and no layer it reports may
        # have refused. What that proves is asymmetric and worth being exact
        # about: the database checks are decisive — no other live project holds
        # this bot — while the two Telegram probes only ever prove *activity*.
        # `_check_no_poller` records `passed=True` for anything that is not a 409
        # (`services/api/src/utils/telegram_token.py:188-191`), so an unreachable
        # or odd Telegram answer reads as "no poller seen", not as "no poller".
        # That is the right default for the route — refusing a run because
        # Telegram had a moment would be worse — and it is why this asserts that
        # every check reported is unrefused rather than that every check proved
        # its subject absent. The run's real protection against a second poller
        # is the binding itself: one project may hold the bot at a time, and
        # teardown releases it.
        assert [check["name"] for check in binding["checks"] if not check["passed"]] == []
        assert {check["name"] for check in binding["checks"]} >= {
            TokenCheckName.TELEGRAM_WEBHOOK,
            TokenCheckName.TELEGRAM_POLLER,
        }, binding

    async def test_the_worker_took_the_scripted_path(self, pipeline):
        """Both tasks carried a change set, and the branch carries their changes.

        The runner falls back to an empty commit when a task document holds no
        change-set block, and an empty commit still makes the branch ahead of
        main — so "ahead by two" proves nothing. The branch's own diff does.
        """
        assert pipeline.get("level1_scripted_path_error") is None, pipeline.get(
            "level1_scripted_path_error"
        )
        scripted = pipeline.get("level1_scripted_path")
        assert scripted, (
            "no scripted-path evidence was recorded; engineering ended "
            f"{pipeline.get('task_status')}"
        )
        assert scripted["paths_missing_from_diff"] == []
        assert len(scripted["change_set_paths"]) >= 5

    async def test_the_deployed_backend_answers_the_endpoint_the_change_set_added(self, pipeline):
        """Honest because the marker is this run's and the kit serves no such route.

        `GET /level1/marker` does not exist in the pinned template, and the
        marker is minted per run, so neither a scaffolded file nor a cached image
        nor an earlier run can answer with it.
        """
        assert pipeline.get("level1_endpoint_probe_error") is None, pipeline.get(
            "level1_endpoint_probe_error"
        )
        probe = pipeline.get("level1_endpoint_probe")
        assert probe, _no_probe(pipeline, "level1_endpoint_probe")
        assert probe["status_code"] == 200, probe
        assert probe["marker"] == pipeline["level1_marker"], probe

    async def test_the_product_scoped_setting_is_registered_on_the_deployment(self, pipeline):
        """Honest because the registry is generated, not written by the change set.

        The change set edits `services/backend/manifest.yaml` and nothing else;
        `SETTINGS_SCHEMAS` is produced from it by `framework.generate` during
        `make setup`, and the offline gate test asserts the change set never
        writes a generated file. So a declaration that reaches this payload
        reached it through the product's own generator, on the deployed image —
        and its declared default is this run's marker, which nothing else has.
        """
        probe = pipeline.get("level1_endpoint_probe")
        assert probe, _no_probe(pipeline, "level1_endpoint_probe")
        assert probe["setting_key"] == LEVEL1_SETTING_KEY, probe
        declared = probe["declared_settings"]
        # Exactly one declared key carries this run's marker as the value the
        # manifest declares for it. The registry key is spelled by the
        # generator, not by the change set, so the assertion names the local key
        # as a suffix rather than assuming how the generator qualifies it — and
        # "exactly one" is what keeps that from being a weaker claim.
        assert len(probe["settings_declaring_marker"]) == 1, declared
        assert probe["settings_declaring_marker"][0].endswith(LEVEL1_SETTING_KEY), declared

    async def test_the_deployed_bot_registered_the_command_handler(self, pipeline):
        """Honest because Telegram is answering for the running bot, not for us.

        The deployed bot publishes its command list with `setMyCommands` in its
        own post-init, using the token this run bound, and the description
        carries this run's marker — so a menu an earlier run left on the same bot
        cannot satisfy this, and nothing but the deployed image could have
        written it. It is the cheapest honest probe available: reading the image
        out of the registry would need a pull and a credential, and exercising
        the handler would need a second Telegram identity the harness has not
        got.
        """
        assert pipeline.get("level1_command_menu_probe_error") is None, pipeline.get(
            "level1_command_menu_probe_error"
        )
        probe = pipeline.get("level1_command_menu_probe")
        assert probe, _no_probe(pipeline, "level1_command_menu_probe")
        assert probe["status_code"] == 200, probe
        assert {
            "command": probe["expected_command"],
            "description": probe["expected_description"],
        } in probe["commands"], probe

    async def test_no_engineering_step_failed(self, pipeline):
        """A green run names no failed step; a red one names which one it was."""
        assert pipeline.get("engineering_failure_steps", {}) == {}

    async def test_project_active(self, pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed, status: {pipeline.get('scaffold_status')}"
        )
        assert pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed, task status: {pipeline.get('task_status')}"
        )
        assert pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed, app_status: {pipeline.get('final_app_status')}"
        )

    async def test_noop_paid_admission_and_settlement_are_durable(self, pipeline):
        """Every deterministic engineering attempt retains its paid-work evidence."""
        assert pipeline.get("noop_settlement_error") is None, pipeline.get("noop_settlement_error")
        settlement = pipeline.get("noop_settlement") or {}
        assert len(settlement) == 2
        for run_id, evidence in settlement.items():
            assert evidence["decision"]["agent_type"] == "noop", run_id
            assert evidence["decision"]["source"] == "project_pin", run_id
            assert evidence["admission"]["outcome"] == "admitted", run_id
            assert evidence["ledger"]["cost_source"] == "unknown", run_id
            assert evidence["ledger"]["cost_microusd"] is None, run_id

    async def test_two_noop_tasks_are_sequenced_reused_and_complete_before_deploy(self, pipeline):
        """A blocked second Task cannot run early or create another Story worker."""
        assert pipeline.get("noop_task_sequence_error") is None, pipeline.get(
            "noop_task_sequence_error"
        )
        assert pipeline.get("first_task_status") == TaskStatus.DONE
        assert pipeline.get("second_task_status") == TaskStatus.DONE
        assert pipeline.get("linear_noop_completion_error") is None, pipeline.get(
            "linear_noop_completion_error"
        )
        assert set(pipeline.get("linear_noop_task_statuses_before_deploy", {}).values()) == {
            TaskStatus.DONE
        }
        assert len(pipeline.get("linear_noop_worker_ids", [])) == 1

    async def test_env_contract_present_on_merged_sha(self, pipeline):
        """The contract also holds on the SHA deploy actually resolves it at.

        Deploy reads the contract at the merged head SHA, not at the scaffolded
        tree, so a fragment lost or broken during engineering only shows here.
        """
        assert pipeline.get("deploy_run_error") is None, pipeline["deploy_run_error"]
        errors = pipeline.get("env_contract_errors") or {}
        assert "merged" not in errors, errors.get("merged")
        probe = pipeline["env_contract_probes"]["merged"]
        assert probe["ref"] == pipeline["deploy_head_sha"]
        assert probe["merged_into_main"] is True, "deploy head SHA is not contained in main"
        assert set(probe["fragment_paths"]) >= EXPECTED_ENV_CONTRACT_FRAGMENTS

    async def test_deploy_run_outcome_success(self, pipeline):
        """The deploy run this mega triggered must conclude deploy_outcome=success.

        A running application only proves some container answers on the port;
        the typed outcome is what the pipeline itself concluded about the deploy.
        """
        assert pipeline.get("deploy_run_error") is None, pipeline["deploy_run_error"]
        assert pipeline.get("deploy_outcome_error") is None, pipeline["deploy_outcome_error"]
        assert pipeline.get("deploy_outcome") == DeployOutcome.SUCCESS.value, (
            f"Deploy run {pipeline.get('deploy_run_id')} ended "
            f"deploy_outcome={pipeline.get('deploy_outcome')} "
            f"({pipeline.get('deploy_error_details')})"
        )

    async def test_health_endpoint(self, pipeline):
        """The externally reachable address answered before the lifecycle teardown."""
        probe = pipeline.get("health_probe_before_undeploy")
        assert probe, "No pre-undeploy health probe was recorded"
        assert probe["url"] == pipeline["deployed_url"]
        assert probe["status_code"] == 200, probe

    async def test_deployed_images_are_the_built_commits(self, pipeline):
        """The deployment runs the images the built commit produced."""
        assert pipeline.get("deployed_image_error") is None, pipeline["deployed_image_error"]
        assert pipeline["deployed_image_references"], "the deploy run named no image references"

    async def test_non_llm_qa_passed(self, pipeline):
        """A separate post-deploy QA run must terminate as passed."""
        assert pipeline.get("qa_result") == {
            "run_id": pipeline["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }

    async def test_story_completed_and_owner_notification_delivered(self, pipeline):
        """QA completion leaves one durable completion record accepted by PO."""
        assert pipeline.get("story_terminal_error") is None, pipeline.get("story_terminal_error")
        assert pipeline.get("story_terminal", {}).get("status") == StoryStatus.COMPLETED.value
        notification = pipeline.get("owner_notification")
        event = pipeline.get("owner_notification_po_event")
        assert pipeline.get("owner_notification_error") is None, pipeline.get(
            "owner_notification_error"
        )
        assert notification and event
        assert notification["event"] == event["event"] == "story_completed"
        assert notification["project_id"] == event["project_id"] == pipeline["project_id"]
        assert notification["story_id"] == event["story_id"] == pipeline["story_id"]
        assert notification["terminal_status"] == StoryStatus.COMPLETED.value
        assert notification["task_id"] is None
        assert event["task_id"] == pipeline["story_id"]
        assert notification["text"] == event["text"]

    async def test_the_owner_message_is_the_one_a_bot_product_sends(self, pipeline):
        """A bot owner is told how to reach their bot, never a backend address.

        The level-1 product is a Telegram bot, so the completion message names
        the bot, repeats the usage examples the owner confirmed — in the
        language they confirmed them in — and gives no server address at all.
        Judged against the brief read back from the API, so it is the frozen
        document the message is compared with and not the harness's constant.
        """
        notification = pipeline.get("owner_notification")
        assert notification, pipeline.get("owner_notification_error")
        content = pipeline["brief_read"]["content"]
        assert (
            bot_completion_message_mismatches(
                notification["text"],
                bot_username=pipeline["bot_username"],
                usage_examples=content["usage_examples"],
                language=content["language"],
            )
            == []
        )
        # Stated separately as well: the address this run actually deployed at
        # is the one a regression would be most likely to reintroduce.
        assert pipeline["deployed_url"] not in notification["text"]

    async def test_the_story_is_backed_by_a_confirmed_brief_built_without_a_model(self, pipeline):
        """The released PO tools froze this contract; no model composed any of it.

        The document is read back over the API rather than out of the PO's own
        rendered message: what a story is planned against is the frozen object,
        and that is what carries the confirmation and the story it backs.
        """
        brief = pipeline["brief_read"]
        assert brief["confirmed_at"]
        assert brief["story_id"] == pipeline["story_id"]
        content = brief["content"]
        assert content["language"] == LEVEL1_BRIEF_LANGUAGE
        assert content["limitations"]
        exemplified = {example["requirement_id"] for example in content["usage_examples"]}
        user_facing = {
            requirement["id"]
            for requirement in content["must_requirements"]
            if requirement["user_facing"]
        }
        assert user_facing and user_facing <= exemplified
        assert content["initial_settings"] == [
            {
                "key": LEVEL1_SETTING_KEY,
                "scope": "product",
                "subject_id": None,
                "value": level1_settings_value(pipeline["level1_marker"]),
                "description": content["initial_settings"][0]["description"],
            }
        ]

    async def test_the_plan_crossed_the_coverage_gate_rather_than_going_round_it(self, pipeline):
        """Admission is what released this story's tasks, and nothing else was.

        Both halves matter. Before the one admission step the tasks existed and
        were undispatchable — that is the gate, read rather than assumed — and
        the admission's own release set is exactly the tasks the story went on
        to run. A plan that had bypassed the gate would show tasks already
        dispatchable in the first snapshot.
        """
        assert [task["dispatch_admitted"] for task in pipeline["level1_plan_before_admission"]] == [
            False,
            False,
        ]
        attempt_id = pipeline["level1_planning_attempt_id"]
        assert {
            task["planning_attempt_id"] for task in pipeline["level1_plan_before_admission"]
        } == {attempt_id}
        admission = pipeline["level1_admission"]
        assert admission["outcome"] == "admitted"
        assert sorted(admission["released_task_ids"]) == sorted(pipeline["task_ids"])
        assert pipeline["brief_read"]["coverage_admitted_at"]
        assert pipeline["brief_read"]["planning_attempt_id"] == attempt_id
        covered = {row["requirement_id"]: row for row in pipeline["level1_coverage"]}
        assert set(covered) == set(pipeline["brief_requirement_ids"])
        assert all(row["task_id"] in pipeline["task_ids"] for row in covered.values())
        assert all(row["returned_reason"] is None for row in covered.values())
        assert [task["dispatch_admitted"] for task in pipeline["level1_plan_after_admission"]] == [
            True,
            True,
        ]

    async def test_nothing_but_this_run_planned_the_story(self, pipeline):
        """The zero-model property is proved from durable rows, not from a won race.

        The level-1 run publishes its story to the live architect consumer, so
        "the harness claimed first" is a race — and a race lost quietly would
        spend an architect model turn on a suite that then passed. Every
        observation the run took says the same three things: the brief's
        planning attempt is this run's (a rival claim would have minted another
        id), the claim was never finished out from under it, and the story
        carries exactly the two tasks this run planned.
        """
        observations = pipeline["level1_plan_provenance"]
        assert [one["when"] for one in observations] == [
            "before_admission",
            "after_admission",
            "after_engineering",
        ]
        attempt_id = pipeline["level1_planning_attempt_id"]
        assert {one["planning_attempt_id"] for one in observations} == {attempt_id}
        assert [one["planning_attempt_active"] for one in observations] == [True, False, False]
        assert all(one["task_ids"] == sorted(pipeline["task_ids"]) for one in observations)
        assert all(one["task_planning_attempt_ids"] == [attempt_id] for one in observations)
        assert pipeline["level1_story_started_by"] in {"harness", "architect"}

    async def test_the_deploy_seeded_the_confirmed_setting_into_the_product(self, pipeline):
        """Three facts, and the third is the only one the product itself says.

        The run result records what the deploy's own write path concluded per
        setting; the deploy consumer's log says which brief it read and by which
        route — `story`, this story's own brief, not the project's latest; and
        the readback is the deployed product answering with the value the user
        confirmed. The confirmed value is deliberately not the manifest default,
        so a product that was never seeded cannot answer with it.
        """
        assert pipeline["level1_settings_seed"] == [
            {
                "key": LEVEL1_SETTING_KEY,
                "scope": "product",
                "subject_id": None,
                "written": True,
                "failure": None,
            }
        ]
        assert pipeline.get("settings_seed_brief_log_error") is None, pipeline.get(
            "settings_seed_brief_log_error"
        )
        assert pipeline["settings_seed_brief_log"] == {
            "event": "deploy_settings_seed_brief",
            "task_id": pipeline["deploy_run_id"],
            "brief_id": pipeline["brief_id"],
            "route": "story",
            "settings_count": 1,
        }
        assert pipeline["level1_settings_readback"] == {
            "contract_version": 1,
            "key": LEVEL1_SETTING_KEY,
            "scope": "product",
            "subject_id": None,
            "value": level1_settings_value(pipeline["level1_marker"]),
        }

    async def test_deployment_sha_and_product_undeploy_lifecycle(self, pipeline):
        """The selected deployment matches merged SHA, then product undeploy clears it."""
        deployment = pipeline.get("service_deployment")
        assert pipeline.get("service_deployment_error") is None, pipeline.get(
            "service_deployment_error"
        )
        assert deployment and deployment["result"] == "success"
        assert deployment["deployed_sha"] == pipeline["deploy_head_sha"]
        assert pipeline.get("application_before_undeploy_error") is None
        assert (
            pipeline.get("application_before_undeploy", {}).get("status")
            == ApplicationStatus.RUNNING.value
        )
        assert pipeline.get("undeploy_request_error") is None, pipeline.get(
            "undeploy_request_error"
        )
        assert pipeline.get("undeploy_run_error") is None, pipeline.get("undeploy_run_error")
        assert pipeline.get("undeploy_run", {}).get("status") == "completed"
        assert pipeline.get("application_after_undeploy_error") is None
        assert (
            pipeline.get("application_after_undeploy", {}).get("status")
            == ApplicationStatus.NOT_DEPLOYED.value
        )
        assert pipeline.get("undeploy_residue_error") is None, pipeline.get(
            "undeploy_residue_error"
        )
        assert pipeline.get("undeploy_residue", {}).get("port_allocation_absent") is True

    async def test_the_run_kept_what_each_developer_attempt_was_told(self, pipeline):
        """Every task's TASK.md is in the evidence, and quotes its criteria verbatim.

        An attempt is one engineering task of the *run*, not of one story and not
        one container: the first story's worker is reused across its two tasks
        and rewrites `/workspace/TASK.md` at the start of each turn, and the
        second story is a third task on a worker of its own. So this asserts
        something only the per-pass capture can produce — three attempts, each
        with the document that attempt was actually handed. A document this run
        could not read is a gap `complete` names, and this test is what says the
        run had none.
        """
        extension = _extension(pipeline)
        instructions = run_evidence.developer_instructions(
            pipeline, pipeline["run_evidence"].records()
        )
        assert [attempt["attempt_id"] for attempt in instructions["attempts"]] == [
            *pipeline["task_ids"],
            *extension["task_ids"],
        ]
        assert instructions["complete"]["status"] == CaptureStatus.CAPTURED.value, instructions[
            "complete"
        ]["reason"]
        # The one assertion about the *content*, and it is the strict helper on
        # purpose. Two claims at once: every attempt of this story *has*
        # acceptance criteria — `admit_level1_plan` plans them from this run's
        # marker — and the document that attempt's developer was actually handed
        # quotes them word for word. The permissive
        # `attempts_not_quoting_acceptance_criteria` would answer `[]` for a plan
        # that asked for nothing, which is exactly how this assertion was vacuous
        # before; it stays for callers whose tasks may genuinely carry none.
        assert run_evidence.attempts_without_quoted_acceptance_criteria(instructions) == []
        # And the criteria really are this run's *and this story's*, so neither a
        # document another run left behind nor the first story's own document —
        # still in the reused workspace when the second story starts — could
        # have satisfied the check above.
        expected_marker = {
            **dict.fromkeys(pipeline["task_ids"], pipeline["level1_marker"]),
            **dict.fromkeys(extension["task_ids"], pipeline["level1_extension_marker"]),
        }
        for attempt in instructions["attempts"]:
            document = attempt["documents"][run_evidence.TASK_DOCUMENT]
            assert (
                expected_marker[attempt["attempt_id"]]
                in document["readings"][0]["body"]["value"]["text"]
            )

    # ── The second story of the same project ─────────────────────────────
    #
    # Everything above is the first story, and everything below is what only
    # the second one can show. The extension story's own facts live under
    # `level1_extension`, because the run's context carries "the current story"
    # under one set of keys and the fixture gives the first story's back once
    # the extension story is done.

    async def test_the_extension_story_is_a_confirmed_correction_of_its_own_revision(
        self, pipeline
    ):
        """The second brief is a *new revision*, opened by correcting the first one.

        The released tool has no update path: `present_product_brief` with
        `corrects_brief_id` opens another revision and moves the project's
        pointer, and the superseded revision stays exactly as it was. All three
        halves are asserted — the confirmed revision is later than the one it
        corrects, it names the story the extension ran, and the corrected
        revision is still unconfirmed and still bound to no story.

        The revision it corrects is the extension's own first presentation, not
        the first story's brief, and that is a property of the released tool
        rather than a choice here: `create_story` spends the project's
        presented-brief pointer when it binds a revision to a story, and
        `present_product_brief` refuses a `corrects_brief_id` while no revision
        is open. A revision already spent on a story is therefore not
        correctable at all — which is what "the superseded revision stays
        exactly as it was" means.
        """
        extension = _extension(pipeline)
        revisions = extension["level1_brief_revisions"]
        assert revisions["confirmed"]["corrects_brief_id"] == revisions["corrected"]["brief_id"]
        assert revisions["confirmed"]["revision"] > revisions["corrected"]["revision"]
        # The first story's brief is the revision before both of them, and this
        # is the project's second story, so the confirmed revision is not the
        # first story's either.
        assert revisions["corrected"]["revision"] > pipeline["brief_read"]["revision"]
        assert revisions["confirmed"]["story_id"] == extension["story_id"] != pipeline["story_id"]
        assert revisions["corrected"]["confirmed_at"] is None
        assert revisions["corrected"]["story_id"] is None
        assert extension["brief_read"]["confirmed_at"]
        assert extension["brief_read"]["content"]["language"] == LEVEL1_BRIEF_LANGUAGE

    async def test_the_extension_plan_crossed_the_coverage_gate_rather_than_going_round_it(
        self, pipeline
    ):
        """The same admission the first story's plan took, for a plan of one task.

        Same two halves as the first story's: before the one admission step the
        task existed and was undispatchable, and the admission's own release set
        is exactly the task the extension story went on to run. No architect
        model was asked anything for the *second* story either, which is the
        claim `nothing but this run planned` makes from durable rows below.
        """
        extension = _extension(pipeline)
        before = extension["level1_plan_before_admission"]
        assert [task["dispatch_admitted"] for task in before] == [False]
        attempt_id = extension["level1_planning_attempt_id"]
        assert attempt_id != pipeline["level1_planning_attempt_id"]
        admission = extension["level1_admission"]
        assert admission["outcome"] == "admitted"
        assert sorted(admission["released_task_ids"]) == sorted(extension["task_ids"])
        assert extension["brief_read"]["coverage_admitted_at"]
        assert extension["brief_read"]["planning_attempt_id"] == attempt_id
        covered = {row["requirement_id"]: row for row in extension["level1_coverage"]}
        assert set(covered) == set(extension["brief_requirement_ids"])
        assert all(row["task_id"] in extension["task_ids"] for row in covered.values())
        assert all(row["returned_reason"] is None for row in covered.values())
        assert [task["dispatch_admitted"] for task in extension["level1_plan_after_admission"]] == [
            True
        ]

    async def test_nothing_but_this_run_planned_the_extension_story(self, pipeline):
        """The zero-model property, proved again for the project's second story.

        The extension story is published to the same live architect consumer the
        first one was, so this is the same race and the same proof from durable
        rows: the brief's planning attempt is this run's, the claim was never
        finished out from under it, and the story carries exactly the one task
        this run planned.
        """
        extension = _extension(pipeline)
        observations = extension["level1_plan_provenance"]
        assert [one["when"] for one in observations] == [
            "before_admission",
            "after_admission",
            "after_engineering",
        ]
        attempt_id = extension["level1_planning_attempt_id"]
        assert {one["planning_attempt_id"] for one in observations} == {attempt_id}
        assert [one["planning_attempt_active"] for one in observations] == [True, False, False]
        assert all(one["task_ids"] == sorted(extension["task_ids"]) for one in observations)
        assert all(one["task_planning_attempt_ids"] == [attempt_id] for one in observations)

    async def test_the_extension_story_ran_in_the_projects_reused_workspace(self, pipeline):
        """The second story got the checkout the first one left behind.

        The manager has two ways to give a worker a workspace and logs them
        differently: `_find_developer_workspace` hands over the project's one
        persistent checkout and *raises* rather than inventing it, while an
        ephemeral directory is a QA executor's and logs another event. So an
        assignment of the scaffolded checkout under this event is the directory
        the scaffolder made for the first story, and every developer worker of
        this repository sharing one path is that directory being reused.

        Asserted of the extension story's *own* worker, not of whichever
        assignments the manager's log tail happens to still hold: the first
        story's line alone must not be able to answer this.
        """
        extension = _extension(pipeline)
        assert extension.get("first_checkout_error") is None, extension.get("first_checkout_error")
        # Judged by the worker that actually ran *this* story — the one the
        # manager checked this story's branch out on. Over every assignment for
        # the repository, the first story's line alone would answer.
        own_workers = {attempt["worker_id"] for attempt in extension["first_checkout"]}
        assert own_workers, "no worker ran a checkout of the extension story's branch"
        assert (
            workspace_reuse_mismatches(
                extension["workspace_assignments"],
                repo_id=pipeline["repo_id"],
                worker_ids=own_workers,
            )
            == []
        )

    async def test_the_extension_story_has_no_failed_or_cancelled_engineering_run(self, pipeline):
        """A done task is not a clean attempt, so the Runs are what is read.

        `issue:028670f21dbd138ccd04` ended with a done task twice: the first
        attempt died on its checkout, the manager deleted the worker, the Run
        failed, and the automatic retry did the work. Reading the task would
        have called both runs green.
        """
        extension = _extension(pipeline)
        assert extension.get("story_engineering_runs_error") is None, extension.get(
            "story_engineering_runs_error"
        )
        assert engineering_run_mismatches(extension["story_engineering_runs"]) == []
        assert extension.get("engineering_failure_steps", {}) == {}
        assert extension["task_status"] == TaskStatus.DONE

    async def test_the_first_checkout_of_the_extension_branch_completed_in_seconds(self, pipeline):
        """The duration is in the evidence as a number, and it is under the bound.

        The bound is `SECOND_STORY_CHECKOUT_BOUND_SECONDS` — fifteen seconds —
        and it is chosen from `issue:028670f21dbd138ccd04`: the checkout that
        broke production sat on the manager's 30-second exec bound until the
        worker was killed, and the retry that worked did the same job in about
        four seconds. Fifteen is nearly four times the work that is known to be
        enough and half the timeout that must never be reached, so a stand under
        load does not fail here and a checkout drifting towards the exec bound
        does.
        """
        extension = _extension(pipeline)
        assert extension.get("first_checkout_error") is None, extension.get("first_checkout_error")
        branch = f"story/{extension['story_id']}"
        attempts = extension["first_checkout"]
        assert (
            checkout_mismatches(
                attempts, branch=branch, bound_seconds=SECOND_STORY_CHECKOUT_BOUND_SECONDS
            )
            == []
        )
        # Stated as its own assertion as well, because "a number a reader can
        # see" is the point: the artifact carries the seconds it took.
        assert isinstance(attempts[0]["duration_seconds"], (int, float))

    async def test_the_extension_branch_was_cut_from_a_main_carrying_the_first_story(
        self, pipeline
    ):
        """Card 1305: the branch was cut from the reused workspace's stale HEAD.

        Containment is proved rather than assumed — GitHub is asked whether the
        commit the branch forked from contains the first story's merge commit,
        and `identical` or `ahead` is the answer that means it does.
        """
        extension = _extension(pipeline)
        assert extension.get("story_branch_base_error") is None, extension.get(
            "story_branch_base_error"
        )
        merge_commit = pipeline["deploy_merge_commit_sha"]
        assert merge_commit, "the first story's deploy named no merge commit"
        assert (
            branch_base_mismatches(
                extension["story_branch_base_probe"], expected_contains=merge_commit
            )
            == []
        )

    async def test_the_manager_ran_no_product_hook_during_the_extension_story(self, pipeline):
        """Card 1305: the manager's own git ran the *product's* pre-push hook.

        Read out of the running manager rather than out of this checkout's
        source: what matters is what the deployed manager does to a workspace
        that carries `core.hooksPath=.githooks` from the first story's
        `make setup`, and an assertion against this tree would pass against an
        image built before the fix.

        Three claims, and the third is the one the run itself contributes: every
        git invocation of the script neutralises the workspace's hooks path per
        command, the script never *writes* that path — so the product's hooks
        keep running for the developer agent — and the checkout that actually
        ran did not fail, which a hook that ran would have made it do.
        """
        extension = _extension(pipeline)
        assert extension.get("manager_checkout_script_error") is None, extension.get(
            "manager_checkout_script_error"
        )
        assert product_hook_mismatches(extension["manager_checkout_script"]) == []
        assert [attempt for attempt in extension["first_checkout"] if attempt["failed"]] == []

    async def test_ci_started_for_the_merge_commit_of_the_extension_story(self, pipeline):
        """The project's own `ci.yml`, for the commit that is actually deployed.

        The deployed commit is the merge commit and never the pull request head,
        and the scheduler records what it observed on the story while it waits
        for that commit's images — so this reads the platform's own observation
        rather than taking a second look at GitHub.
        """
        extension = _extension(pipeline)
        assert extension.get("story_ci_runs_error") is None, extension.get("story_ci_runs_error")
        assert (
            ci_run_mismatches(
                extension["story_ci_runs"],
                merge_commit_sha=extension["deploy_merge_commit_sha"],
            )
            == []
        )

    async def test_the_extension_deploy_took_the_pr_poller_and_the_first_took_the_grant(
        self, pipeline
    ):
        """Which path deployed, asserted — not inferred from the deploy succeeding.

        The two paths are the project's two situations. Its first story has no
        owner yet, so a `tg_bot` product's first deploy goes through the
        API-owned initial-owner grant lifecycle; every story after it deploys
        through the scheduler's merged-PR poller, which is the ordinary path.
        Both are asserted here, and the contrast is what makes either assertion
        worth anything: a run where both stories took the same path fails,
        whichever path that is.
        """
        extension = _extension(pipeline)
        assert (
            deploy_path_mismatches(pipeline["deploy_path"], expected=DEPLOY_PATH_OWNER_GRANT) == []
        )
        assert (
            deploy_path_mismatches(extension["deploy_path"], expected=DEPLOY_PATH_PR_POLLER) == []
        )
        assert extension["deploy_path"]["run_id"] != pipeline["deploy_path"]["run_id"]
        assert extension["deploy_outcome"] == DeployOutcome.SUCCESS.value

    async def test_the_deployment_carries_both_stories_and_the_corrected_settings(self, pipeline):
        """What the *deployed product* says about the second story, and the first.

        The pinned kit serves no `/level1/extension`; both markers are minted per
        run; and the payload carries the first story's marker beside the
        extension's, so the answer is a deployment carrying both stories' work.
        The setting is the other half: the extension brief confirmed a second
        key, the deploy seeded it through this story's own brief, and the value
        read back off the product exists nowhere but in that corrected brief.
        """
        extension = _extension(pipeline)
        assert extension.get("level1_extension_endpoint_probe_error") is None, extension.get(
            "level1_extension_endpoint_probe_error"
        )
        probe = extension["level1_extension_endpoint_probe"]
        assert probe["status_code"] == 200, probe
        assert probe["url"].endswith(LEVEL1_EXTENSION_ENDPOINT_PATH)
        assert probe["marker"] == pipeline["level1_extension_marker"], probe
        assert probe["base_marker"] == pipeline["level1_marker"], probe
        assert probe["settings_declaring_marker"] == [LEVEL1_EXTENSION_SETTING_KEY], probe
        assert extension["level1_settings_seed"] == [
            {
                "key": LEVEL1_EXTENSION_SETTING_KEY,
                "scope": "product",
                "subject_id": None,
                "written": True,
                "failure": None,
            }
        ]
        assert extension.get("settings_seed_brief_log_error") is None, extension.get(
            "settings_seed_brief_log_error"
        )
        assert extension["settings_seed_brief_log"] == {
            "event": "deploy_settings_seed_brief",
            "task_id": extension["deploy_run_id"],
            "brief_id": extension["brief_id"],
            "route": "story",
            "settings_count": 1,
        }
        assert extension["level1_settings_readback"] == {
            "contract_version": 1,
            "key": LEVEL1_EXTENSION_SETTING_KEY,
            "scope": "product",
            "subject_id": None,
            "value": level1_extension_settings_value(pipeline["level1_extension_marker"]),
        }

    async def test_the_extension_story_completed_with_a_second_owner_notification(self, pipeline):
        """QA passed, the story completed, and its owner was told — again.

        The second notification is judged exactly the way card 1312 judges the
        first: a bot product's message, naming the bot, carrying the usage
        examples of *this* story's confirmed brief in that brief's language, and
        giving no server address. It is the extension brief's examples, so the
        first story's message could not satisfy it — and the two messages are
        asserted to differ, which is what makes this the second notification
        rather than the first one read twice.
        """
        extension = _extension(pipeline)
        assert extension["qa_result"] == {
            "run_id": extension["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }
        assert extension.get("story_terminal_error") is None, extension.get("story_terminal_error")
        assert extension["story_terminal"]["status"] == StoryStatus.COMPLETED.value
        assert extension.get("owner_notification_error") is None, extension.get(
            "owner_notification_error"
        )
        notification = extension["owner_notification"]
        event = extension["owner_notification_po_event"]
        assert notification["event"] == event["event"] == "story_completed"
        assert notification["story_id"] == event["story_id"] == extension["story_id"]
        assert notification["terminal_status"] == StoryStatus.COMPLETED.value
        assert notification["text"] == event["text"]
        assert notification["text"] != pipeline["owner_notification"]["text"]
        content = extension["brief_read"]["content"]
        assert (
            bot_completion_message_mismatches(
                notification["text"],
                bot_username=pipeline["bot_username"],
                usage_examples=content["usage_examples"],
                language=content["language"],
            )
            == []
        )
        assert pipeline["deployed_url"] not in notification["text"]


class TestFullPipelineLLM:
    """THE MEGA TEST with a real developer worker."""

    async def test_project_active(self, llm_pipeline):
        """Project status should be 'active' after successful scaffold + deploy."""
        assert llm_pipeline.get("agent_type") == os.getenv("LIVE_WORKER_AGENT_TYPE", "claude")
        assert llm_pipeline.get("scaffold_status") == ProjectStatus.ACTIVE, (
            f"Scaffold failed, status: {llm_pipeline.get('scaffold_status')}"
        )
        assert llm_pipeline.get("task_status") == TaskStatus.DONE, (
            f"Engineering failed, task status: {llm_pipeline.get('task_status')}"
        )
        assert llm_pipeline.get("final_app_status") == ApplicationStatus.RUNNING.value, (
            f"Deploy failed, app_status: {llm_pipeline.get('final_app_status')}"
        )

    async def test_story_branch_carries_the_worker_commit(self, llm_pipeline):
        """The paid task must leave a commit, and that is asserted before deploy.

        A story branch that is not ahead of main means the worker committed
        nothing: the story PR is refused 422 and no deploy run can ever exist.
        The suite says so here rather than expiring the deploy wait and
        reporting the missing run as if it were the reason.
        """
        if llm_pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        assert llm_pipeline.get("story_branch_error") is None, llm_pipeline["story_branch_error"]
        compare = llm_pipeline.get("story_branch_compare") or {}
        assert compare.get("ahead_by", 0) >= 1, compare

    async def test_health_payload_carries_this_run_marker(self, llm_pipeline):
        """The change the task asked for is visible on the deployed service.

        The marker is minted per run, so no scaffolded file and no artifact of
        an earlier run can answer for this one: seeing it in the live payload is
        what proves this run's worker made an observable change.
        """
        probe = llm_pipeline.get("health_probe_before_undeploy")
        if not probe:
            pytest.skip("no health probe was recorded")
        assert probe.get("marker_present") is True, (
            f"GET {probe['endpoint']} does not carry {llm_pipeline['health_marker']}: {probe}"
        )

    async def test_requested_qa_executor_is_active(self, llm_pipeline):
        if not llm_pipeline.get("qa_requires_executor"):
            pytest.skip("ordinary mega uses deterministic health-only QA")
        assert llm_pipeline.get("qa_agent_type") == os.environ["LIVE_QA_AGENT_TYPE"]

    async def test_no_user_secrets_required(self, llm_pipeline):
        """The backend-only LLM project must not trip the user-secret deploy path.

        Only *required* user secrets dead-end the deploy (DeployOutcome
        WAITING_FOR_USER_SECRET). Optional ``user_secret`` overrides such as the
        template's ``DATABASE_URL`` (``required: false``) are resolved from the
        allocated infrastructure and must not fail this project.
        """
        if llm_pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        errors = llm_pipeline.get("env_contract_errors") or {}
        assert "merged" not in errors, errors.get("merged")
        probe = llm_pipeline["env_contract_probes"]["merged"]
        assert probe["required_user_secret_entries"] == [], (
            f"required user secrets would dead-end deploy: {probe['required_user_secret_entries']}"
        )

    async def test_deploy_run_outcome_success(self, llm_pipeline):
        """The deploy run this mega triggered must conclude deploy_outcome=success."""
        if llm_pipeline.get("task_status") != TaskStatus.DONE:
            pytest.skip("engineering failed")
        assert llm_pipeline.get("deploy_run_error") is None, llm_pipeline["deploy_run_error"]
        assert llm_pipeline.get("deploy_outcome_error") is None, llm_pipeline[
            "deploy_outcome_error"
        ]
        assert llm_pipeline.get("deploy_outcome") == DeployOutcome.SUCCESS.value, (
            f"Deploy run {llm_pipeline.get('deploy_run_id')} ended "
            f"deploy_outcome={llm_pipeline.get('deploy_outcome')} "
            f"({llm_pipeline.get('deploy_error_details')})"
        )

    async def test_health_endpoint(self, llm_pipeline):
        """GET /health evidence is recorded while the LLM deployment runs."""
        probe = llm_pipeline.get("health_probe_before_undeploy")
        assert probe, "No health probe was recorded"
        assert probe["status_code"] == 200, probe

    async def test_deployed_image_tag_is_the_built_commit_before_qa_runs(self, llm_pipeline):
        """QA is only worth spending once the right code is proven to be running.

        Paid run 33753667796 is why this is asserted here and not only inside the
        deploy path: the deploy reported success, the service answered HTTP 200,
        and the wrong code was running. Only the marker assertion, several steps
        and one paid QA turn later, said so.

        The expectation comes from GitHub — the commit `main` points at, which is
        the commit the project's CI built — not from the deploy's own input. An
        assertion derived from what the resolver was given agrees with itself and
        would pass on the wrong tag.
        """
        if (
            llm_pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value
            or llm_pipeline.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        ):
            pytest.skip("deploy failed")
        assert llm_pipeline.get("deployed_image_error") is None, llm_pipeline[
            "deployed_image_error"
        ]
        built_sha = llm_pipeline["main_head_probe"]["sha"]
        expected = llm_pipeline["deployed_image_tag_expected"]
        assert expected.endswith(built_sha[:7]), (
            f"the expected tag {expected} was not derived from main's head {built_sha}"
        )
        references = llm_pipeline.get("deployed_image_references")
        assert references, "the deploy run named no image references"
        assert all(reference.endswith(f":{expected}") for reference in references.values()), (
            f"deployed images {references} are not tagged {expected} for the built commit "
            f"{built_sha}"
        )
        assert llm_pipeline.get("deployed_commit_sha") == built_sha, (
            f"the deploy says it deployed {llm_pipeline.get('deployed_commit_sha')}, "
            f"main points at {built_sha}"
        )

    async def test_non_llm_qa_passed(self, llm_pipeline):
        """A separate post-deploy QA run must terminate as passed."""
        if (
            llm_pipeline.get("final_app_status") != ApplicationStatus.RUNNING.value
            or llm_pipeline.get("deploy_outcome") != DeployOutcome.SUCCESS.value
        ):
            pytest.skip("deploy failed")
        assert llm_pipeline.get("qa_result") == {
            "run_id": llm_pipeline["qa_result"]["run_id"],
            "status": "completed",
            "qa_outcome": "passed",
        }
