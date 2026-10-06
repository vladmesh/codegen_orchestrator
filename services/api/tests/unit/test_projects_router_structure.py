"""Regression coverage for the projects router package boundary."""

import pytest

from src.main import app


@pytest.fixture(scope="module")
def schema():
    """The OpenAPI document, built once: building it walks every route of the app."""
    return app.openapi()


def test_projects_route_table_keeps_its_public_surface(schema):
    routes = {
        (method.upper(), path, operation["operationId"], tuple(operation["responses"]))
        for path, item in schema["paths"].items()
        if path.startswith("/api/projects")
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }

    assert routes == {
        ("POST", "/api/projects/", "create_project_api_projects__post", ("201", "422")),
        ("GET", "/api/projects/", "list_projects_api_projects__get", ("200", "422")),
        (
            "GET",
            "/api/projects/{project_id}",
            "get_project_api_projects__project_id__get",
            ("200", "422"),
        ),
        (
            "PUT",
            "/api/projects/{project_id}",
            "update_project_api_projects__project_id__put",
            ("200", "422"),
        ),
        (
            "PATCH",
            "/api/projects/{project_id}",
            "patch_project_api_projects__project_id__patch",
            ("200", "422"),
        ),
        (
            "PATCH",
            "/api/projects/{project_id}/config",
            "patch_project_config_api_projects__project_id__config_patch",
            ("200", "422"),
        ),
        (
            "DELETE",
            "/api/projects/{project_id}",
            "delete_project_api_projects__project_id__delete",
            ("204", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/config/secrets/keys",
            "list_secret_keys_api_projects__project_id__config_secrets_keys_get",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/config/secrets",
            "merge_secrets_api_projects__project_id__config_secrets_post",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/users/grant",
            "grant_user_api_projects__project_id__users_grant_post",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/ownership-transfer",
            "transfer_ownership_api_projects__project_id__ownership_transfer_post",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/users/grant-intents/lifecycle",
            "resume_initial_owner_intent_api_projects__project_id__users_grant_intents_lifecycle_post",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/users/initial-owner-deployment",
            "get_initial_owner_deployment_api_projects__project_id__users_initial_owner_deployment_get",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/users/grant-intents/{intent_id}/retry",
            "retry_initial_owner_deployment_api_projects__project_id__users_grant_intents__intent_id__retry_post",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/users/grant-intents/{intent_id}",
            "get_intent_api_projects__project_id__users_grant_intents__intent_id__get",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/users/grant-intents/{intent_id}/complete",
            "complete_intent_api_projects__project_id__users_grant_intents__intent_id__complete_post",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/telegram/token",
            "bind_telegram_token_api_projects__project_id__telegram_token_post",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/telegram/liveness",
            "check_telegram_bot_liveness_api_projects__project_id__telegram_liveness_get",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/qa-probes",
            "list_qa_probes_api_projects__project_id__qa_probes_get",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/qa-probes/from-run",
            "store_qa_probes_from_run_api_projects__project_id__qa_probes_from_run_post",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/verification-gaps",
            "list_verification_gaps_api_projects__project_id__verification_gaps_get",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/verification-gaps/from-run",
            "record_verification_gaps_from_run_api_projects__project_id__verification_gaps_from_run_post",
            ("200", "422"),
        ),
        (
            "DELETE",
            "/api/projects/{project_id}/config/secrets/{key}",
            "delete_secret_api_projects__project_id__config_secrets__key__delete",
            ("200", "422"),
        ),
        (
            "POST",
            "/api/projects/{project_id}/teardown",
            "teardown_project_api_projects__project_id__teardown_post",
            ("200", "422"),
        ),
        (
            "GET",
            "/api/projects/{project_id}/teardown",
            "get_teardown_status_api_projects__project_id__teardown_get",
            ("200", "422"),
        ),
    }
