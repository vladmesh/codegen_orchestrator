from pydantic import ValidationError
import pytest

from shared.contracts.dto.users_grant import (
    GrantIntent,
    GrantIntentDispatchTarget,
    GrantIntentKind,
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
    GrantIntentStatus,
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
