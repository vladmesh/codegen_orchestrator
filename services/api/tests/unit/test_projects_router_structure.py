"""Regression coverage for the projects router package boundary."""

from pathlib import Path

from src.main import app

API_SRC = Path(__file__).parents[2] / "src"
ROUTERS = API_SRC / "routers"


def test_projects_router_is_a_thin_package_without_duplicate_guards():
    assert not (ROUTERS / "projects.py").exists()
    for module in (
        "__init__.py",
        "access.py",
        "lifecycle.py",
        "secrets.py",
        "telegram.py",
        "teardown.py",
    ):
        assert (ROUTERS / "projects" / module).exists()

    package = ROUTERS / "projects"
    facade = (package / "__init__.py").read_text()
    assert 'APIRouter(prefix="/projects", tags=["projects"])' in facade
    assert "def _check_project_access" not in facade
    assert "def _load_locked_project" not in facade
    for module in package.glob("*.py"):
        source = module.read_text()
        assert "def _check_project_access" not in source
        assert "def _load_locked_project" not in source

    guards = (ROUTERS / "projects_guards.py").read_text()
    assert "def check_project_access" in guards
    assert "def load_locked_project" in guards


def test_projects_route_table_keeps_its_public_surface():
    schema = app.openapi()
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
            "/api/projects/{project_id}/ownership-transfer/{run_id}/apply",
            "apply_transfer_api_projects__project_id__ownership_transfer__run_id__apply_post",
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
