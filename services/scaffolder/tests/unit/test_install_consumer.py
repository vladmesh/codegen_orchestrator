"""Redelivery and cancellation settle an owned install of either route without engineering."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.catalog_install import InstallPreflight
from shared.contracts.dto.install_operation import InstallDecision, InstallOperation
from shared.contracts.queues.scaffold import ScaffoldMessage
from src.consumer import _process_install_mode
from tests.unit.test_install_executor import LOCAL, message

CYCLE = datetime(2026, 1, 1, tzinfo=UTC)


def local_message(**changes):
    """An admitted product-local INSTALL as the scheduler queues it."""
    owner = {
        "project_id": str(LOCAL.project_id),
        "repository_id": LOCAL.repository_id,
        "cycle_started_at": CYCLE,
        "install": LOCAL.model_dump(mode="json"),
    }
    return ScaffoldMessage.model_validate(message().model_dump() | owner | changes)


@pytest.fixture(params=["catalog", "package_source"])
def owned(request):
    return message() if request.param == "catalog" else local_message()


def decision(msg, *, state="running"):
    return InstallDecision(
        outcome="claimed" if state == "running" else "settled",
        operation=InstallOperation(
            id=msg.operation_id,
            project_id=msg.project_id,
            task_id=msg.task_id,
            story_id=msg.story_id,
            repository_id=msg.repository_id,
            cycle_started_at=msg.cycle_started_at,
            state=state,
            stage="claimed" if state == "running" else "published",
        ),
        install=msg.install,
        git_url="https://github.com/owner/notes",
    )


async def consume(msg, api):
    return await _process_install_mode(
        msg, "owner/notes", AsyncMock(), "synthetic-github", api, SimpleNamespace(), MagicMock()
    )


@pytest.mark.asyncio
async def test_terminal_redelivery_never_touches_product_or_github(owned):
    msg = owned
    api = AsyncMock()
    api.catalog_install_command.return_value = decision(msg, state="published")
    with patch("src.install.run_install", AsyncMock()) as execute:
        assert (await consume(msg, api))["status"] == "skipped"
    execute.assert_not_awaited()
    assert api.catalog_install_command.call_args.args[1].action == "claim"


@pytest.mark.asyncio
async def test_lost_claim_response_reuses_one_token_then_records_cancellation(owned):
    msg = owned
    api = AsyncMock()
    api.catalog_install_command.side_effect = [
        TimeoutError("response lost"),
        decision(msg),
        InstallDecision(outcome="settled"),
    ]
    with patch("src.install.run_install", AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await consume(msg, api)
    bodies = [call.args[1] for call in api.catalog_install_command.call_args_list]
    assert [body.action for body in bodies] == ["claim", "claim", "refuse"]
    assert len({body.token for body in bodies}) == 1 and bodies[0].token
    assert {body.operation_id for body in bodies} == {msg.operation_id}
    assert bodies[-1].stage == "cancelled"


@pytest.mark.asyncio
async def test_forged_project_lease_cannot_execute_an_owned_install():
    owned = message()
    delivery = owned.model_copy(update={"project_id": "foreign-project"})
    api = AsyncMock()
    api.catalog_install_command.side_effect = [decision(owned), InstallDecision(outcome="settled")]
    with patch("src.install.run_install", AsyncMock()) as execute:
        assert (await consume(delivery, api))["reason"] == "message_ownership_mismatch"
    execute.assert_not_awaited()
    assert api.catalog_install_command.call_args.args[1].action == "refuse"


@pytest.mark.asyncio
async def test_publication_retains_native_stages_for_redacted_stand_proof():
    msg = message()
    api = AsyncMock()
    api.catalog_install_command.return_value = decision(msg)
    stages = [
        {
            "stage": "push",
            "argv": ["git", "-c", "core.hooksPath=/dev/null", "push"],
            "returncode": 0,
        }
    ]
    log = MagicMock()
    with patch(
        "src.install.run_install",
        AsyncMock(
            return_value=SimpleNamespace(
                head_sha="a" * 40,
                stages=stages,
                checkout="repo-1/install-1",
                checkout_removed=True,
            )
        ),
    ):
        result = await _process_install_mode(
            msg, "owner/notes", AsyncMock(), "synthetic-github", api, SimpleNamespace(), log
        )
    assert result["status"] == "success"
    assert log.info.call_args.kwargs["execution_stages"] == stages
    assert log.info.call_args.kwargs["checkout"] == "repo-1/install-1"
    assert "synthetic-github" not in str(log.info.call_args)


@pytest.mark.asyncio
async def test_a_glue_answer_is_saved_at_its_operation_as_a_typed_refusal():
    from src.install import InstallExecutionError
    from tests.unit.test_install_executor import check_install, glue_item

    msg = message()
    api = AsyncMock()
    api.catalog_install_command.side_effect = [decision(msg), InstallDecision(outcome="settled")]
    preflight = InstallPreflight.model_validate(
        check_install("glue", [glue_item("command_conflict", symbol="handle_remind")])
    )
    refusal = InstallExecutionError(
        "preflight", "glue_required: command_conflict", base_sha="b" * 40, preflight=preflight
    )
    with patch("src.install.run_install", AsyncMock(side_effect=refusal)):
        result = await consume(msg, api)
    assert result == {"status": "failed", "stage": "preflight", "operation_id": msg.operation_id}
    refused = api.catalog_install_command.call_args.args[1]
    assert refused.action == "refuse" and refused.stage == "preflight"
    assert refused.preflight == preflight and refused.base_sha == "b" * 40


@pytest.mark.asyncio
async def test_the_executor_runs_the_claimed_operation_with_its_admitted_target(owned):
    from src.install import InstallContext

    msg = owned
    api = AsyncMock()
    api.catalog_install_command.return_value = decision(msg)
    published = SimpleNamespace(head_sha="a" * 40, stages=[], checkout="x", checkout_removed=True)
    with patch("src.install.run_install", AsyncMock(return_value=published)) as execute:
        assert (await consume(msg, api))["status"] == "success"
    assert execute.call_args.args[0] == InstallContext(
        project_id=msg.project_id,
        repository_id=msg.repository_id,
        story_id=msg.story_id,
        operation_id=msg.operation_id,
        template_ref=msg.template_ref,
        install=msg.install,
    )


@pytest.mark.asyncio
async def test_a_live_owner_elsewhere_refuses_the_claim_without_execution(owned):
    api = AsyncMock()
    api.catalog_install_command.return_value = InstallDecision(
        outcome="refused", reason="operation_running", operation=decision(owned).operation
    )
    with patch("src.install.run_install", AsyncMock()) as execute:
        assert await consume(owned, api) == {"status": "skipped", "reason": "operation_running"}
    execute.assert_not_awaited()
    [call] = api.catalog_install_command.call_args_list
    assert call.args[1].action == "claim"


OTHER_PRODUCT = "00000000-0000-4000-8000-0000000000b2"


@pytest.mark.parametrize(
    "delivered",
    [
        {"install": LOCAL.model_dump(mode="json") | {"commit": "e" * 40}},
        {"install": LOCAL.model_dump(mode="json") | {"path": "packages/other"}},
        {
            "install": LOCAL.model_dump(mode="json")
            | {"package": LOCAL.package.model_dump() | {"version": "1.0.1"}}
        },
        {"install": LOCAL.model_dump(mode="json") | {"tooling_commit": "f" * 40}},
        {
            "project_id": OTHER_PRODUCT,
            "repository_id": "repo-2",
            "install": LOCAL.model_dump(mode="json")
            | {"project_id": OTHER_PRODUCT, "repository_id": "repo-2"},
        },
        {"story_id": "story-2"},
        {"cycle_started_at": "2020-01-01T00:00:00Z"},
        {"operation_id": "install-2"},
        {"project_name": "another"},
    ],
    ids=[
        "commit",
        "path",
        "package",
        "tooling",
        "same-buyer-product",
        "story",
        "cycle",
        "operation",
        "git-url",
    ],
)
@pytest.mark.asyncio
async def test_a_delivery_differing_from_its_claimed_local_operation_never_executes(delivered):
    admitted = local_message()
    delivery = local_message(**delivered)
    api = AsyncMock()
    api.catalog_install_command.side_effect = [
        decision(admitted),
        InstallDecision(outcome="settled"),
    ]
    with patch("src.install.run_install", AsyncMock()) as execute:
        result = await _process_install_mode(
            delivery,
            f"owner/{delivery.project_name}",
            AsyncMock(),
            "synthetic-github",
            api,
            SimpleNamespace(),
            MagicMock(),
        )
    assert result == {"status": "failed", "reason": "message_ownership_mismatch"}
    execute.assert_not_awaited()
    bodies = [call.args[1] for call in api.catalog_install_command.call_args_list]
    assert [body.action for body in bodies] == ["claim", "refuse"]
    assert bodies[-1].detail == "message_ownership_mismatch"


@pytest.mark.asyncio
async def test_a_local_executor_refusal_is_settled_on_its_own_operation():
    from src.install import InstallExecutionError

    msg = local_message()
    api = AsyncMock()
    api.catalog_install_command.side_effect = [decision(msg), InstallDecision(outcome="settled")]
    refusal = InstallExecutionError(
        "preflight", "source_base_mismatch: replan against the current head", None, "b" * 40
    )
    with patch("src.install.run_install", AsyncMock(side_effect=refusal)):
        result = await consume(msg, api)
    assert result == {"status": "failed", "stage": "preflight", "operation_id": msg.operation_id}
    claim, refused = [call.args[1] for call in api.catalog_install_command.call_args_list]
    assert (refused.action, refused.stage, refused.base_sha) == ("refuse", "preflight", "b" * 40)
    assert refused.detail.startswith("source_base_mismatch")
    assert (refused.operation_id, refused.token) == (msg.operation_id, claim.token)
