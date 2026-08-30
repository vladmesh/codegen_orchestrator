from shared.contracts.dto.users_grant import GrantIntent, GrantIntentKind, GrantIntentStatus


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
