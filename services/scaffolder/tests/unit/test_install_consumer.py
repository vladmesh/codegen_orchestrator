"""Redelivery and cancellation settle an owned install without invoking engineering."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.catalog_install import (
    InstallDecision,
    InstallOperation,
    InstallPreflight,
)
from src.consumer import _process_install_mode
from tests.unit.test_install_executor import message


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
async def test_terminal_redelivery_never_touches_product_or_github():
    msg = message()
    api = AsyncMock()
    api.catalog_install_command.return_value = decision(msg, state="published")
    with patch("src.install.run_install", AsyncMock()) as execute:
        assert (await consume(msg, api))["status"] == "skipped"
    execute.assert_not_awaited()
    assert api.catalog_install_command.call_args.args[1].action == "claim"


@pytest.mark.asyncio
async def test_lost_claim_response_reuses_one_token_then_records_cancellation():
    msg = message()
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
