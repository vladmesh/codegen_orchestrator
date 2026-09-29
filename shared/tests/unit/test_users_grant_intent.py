from pydantic import ValidationError
import pytest

from shared.contracts.dto.users_grant import (
    GrantIntent,
    GrantIntentDispatchTarget,
    GrantIntentExhaustion,
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
    GrantIntentRetryCommand,
    GrantIntentStatus,
)


def test_exhaustion_action_requires_the_admitted_run_fence():
    zero = GrantIntentExhaustion(
        attempts=0,
        target=GrantIntentDispatchTarget(sha="a" * 40),
        exhausted_execution_run_id=None,
        action=None,
        retry_command=None,
    )
    assert zero.action is None and zero.retry_command is None
    with pytest.raises(ValidationError, match="action and retry command must agree"):
        GrantIntentExhaustion.model_validate(
            zero.model_dump(mode="json") | {"action": "retry_initial_owner_deployment"}
        )
    with pytest.raises(ValidationError, match="exhausted admitted Run"):
        GrantIntentExhaustion(
            attempts=0,
            target=GrantIntentDispatchTarget(sha="a" * 40),
            exhausted_execution_run_id=None,
            action="retry_initial_owner_deployment",
            retry_command=GrantIntentRetryCommand(expected_execution_run_id="invented-run"),
        )


def test_grant_intent_is_non_secret_and_binds_one_immutable_target():
    intent = GrantIntent(
        id="grant-1",
        kind=GrantIntentKind.ADD_USER,
        project_id="project-1",
        channel="telegram",
        external_id="84",
        target_application_id=7,
        target_deployment_id=9,
        target_sha="a" * 40,
        initiating_actor="user:42",
    )

    stored = intent.model_dump(mode="json")
    assert stored["status"] == GrantIntentStatus.PUBLISH_OWED.value
    assert set(stored).isdisjoint({"capability", "token", "secret_values", "audience"})


def test_lifecycle_result_exposes_an_attempt_only_when_this_call_dispatched_it():
    dispatched = GrantIntentLifecycleResult(
        intent_id="grant-1",
        status=GrantIntentStatus.QUEUED,
        disposition=GrantIntentLifecycleDisposition.DISPATCHED,
        execution_run_id="deploy-grant-1",
        target=GrantIntentDispatchTarget(sha="a" * 40),
    )
    assert dispatched.execution_run_id == "deploy-grant-1"

    applied = GrantIntentLifecycleResult(
        intent_id="grant-1",
        status=GrantIntentStatus.APPLIED,
        disposition=GrantIntentLifecycleDisposition.ALREADY_APPLIED,
    )
    assert applied.execution_run_id is None

    exhausted = GrantIntentLifecycleResult(
        intent_id="grant-1",
        status=GrantIntentStatus.FAILED,
        disposition=GrantIntentLifecycleDisposition.EXHAUSTED,
    )
    assert exhausted.execution_run_id is None

    stale = GrantIntentLifecycleResult(
        intent_id="grant-1",
        status=GrantIntentStatus.RETRYABLE,
        disposition=GrantIntentLifecycleDisposition.STALE_TARGET,
    )
    assert stale.execution_run_id is None

    with pytest.raises(ValidationError, match="only dispatched"):
        GrantIntentLifecycleResult(
            intent_id="grant-1",
            status=GrantIntentStatus.APPLIED,
            disposition=GrantIntentLifecycleDisposition.ALREADY_APPLIED,
            execution_run_id="deploy-grant-old",
        )
