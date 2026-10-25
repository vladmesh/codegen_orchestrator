"""A product-local INSTALL target is typed, owned and never read as a catalog release.

A FEATURE's package build intent is a distinct, commit-free shape beside it.
"""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.local_install import (
    LocalPackageBuild,
    LocalPackageInstall,
    parse_install_target,
)
from shared.contracts.dto.task import TaskCreate, TaskDTO

PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def local_install(**changes) -> dict:
    return {
        "route": "package_source",
        "project_id": str(PROJECT),
        "repository_id": "repo-1",
        "commit": "1" * 40,
        "path": "packages/notes_digest",
        "package": {
            "name": "notes-digest",
            "distribution": "codegen-kit-notes-digest",
            "version": "0.1.0",
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "tooling_commit": "2" * 40,
        **changes,
    }


def catalog_install() -> dict:
    return {
        "package": {
            "name": "reminders",
            "distribution": "codegen-kit-reminders",
            "version": "0.5.0",
            "tag": "packages/reminders/v0.5.0",
        },
        "libraries": [],
        "binding": {
            "package": "reminders",
            "resource": "codegen_kit_reminders:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": [],
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "catalog_digest": "b" * 64,
        "tooling_commit": "c" * 40,
        "catalog": {
            "repository": "https://github.com/vladmesh/codegen-product-kit.git",
            "commit": "d" * 40,
            "catalog_sha256": "e" * 64,
        },
    }


def create(payload: dict, **changes) -> TaskCreate:
    return TaskCreate(
        **{
            "project_id": PROJECT,
            "title": "Install notes digest",
            "type": "install",
            "story_id": "story-1",
            "repository_id": "repo-1",
            "install": payload,
            **changes,
        }
    )


def read(task: TaskCreate) -> TaskDTO:
    now = datetime.now(UTC)
    return TaskDTO.model_validate(
        {
            **task.model_dump(mode="json"),
            "id": "task-1",
            "current_iteration": 0,
            "dispatch_admitted": True,
            "created_at": now,
            "updated_at": now,
        }
    )


def test_a_local_target_keeps_its_exact_identity_through_create_and_read():
    stored = create(local_install()).model_dump(mode="json")["install"]
    assert stored == local_install()
    target = read(create(local_install())).install
    assert isinstance(target, LocalPackageInstall)
    assert target.model_dump(mode="json") == local_install()


def test_a_catalog_payload_is_still_read_as_its_released_closure():
    target = read(create(catalog_install())).install
    assert isinstance(target, CatalogInstall)
    assert target.model_dump(mode="json") == catalog_install()


@pytest.mark.parametrize(
    "payload",
    [
        local_install(catalog=catalog_install()["catalog"]),
        local_install(catalog_digest="b" * 64),
        local_install(binding=catalog_install()["binding"]),
        local_install(libraries=[]),
        local_install(package={**local_install()["package"], "tag": "packages/x/v0.1.0"}),
        local_install(route="catalog"),
        {**catalog_install(), "route": "package_source"},
        {**catalog_install(), "path": "packages/reminders"},
    ],
    ids=[
        "local-with-catalog",
        "local-with-digest",
        "local-with-binding",
        "local-with-libraries",
        "local-with-tag",
        "catalog-route-name",
        "catalog-with-route",
        "catalog-with-path",
    ],
)
def test_a_payload_mixing_catalog_and_local_fields_is_refused(payload):
    with pytest.raises(ValidationError):
        parse_install_target(payload)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/packages/notes",
        ".",
        "..",
        "./packages",
        "packages/../../etc",
        "packages/..",
        "packages//notes",
        "packages/",
        "packages\\notes",
        "packages/no\x00tes",
        "packages/no\ntes",
        "packages/no\x85tes",
    ],
)
def test_a_package_path_outside_the_normalized_product_tree_is_refused(path):
    with pytest.raises(ValidationError):
        parse_install_target(local_install(path=path))


@pytest.mark.parametrize("path", ["notes_digest", "packages/notes_digest", "a/b.c/d-e"])
def test_a_normalized_product_relative_path_is_accepted(path):
    assert parse_install_target(local_install(path=path)).path == path


@pytest.mark.parametrize("commit", ["main", "1" * 39, "1" * 41, "A" * 40, "refs/heads/x"])
def test_the_source_is_one_full_immutable_commit(commit):
    with pytest.raises(ValidationError):
        parse_install_target(local_install(commit=commit))


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": uuid.uuid4()},
        {"repository_id": "repo-2"},
    ],
    ids=["other-product", "other-repository"],
)
def test_a_local_task_names_the_source_of_its_own_product_repository(changes):
    with pytest.raises(ValidationError):
        create(local_install(), **changes)


@pytest.mark.parametrize("field", ["story_id", "repository_id", "install"])
def test_a_local_install_still_requires_story_repository_and_payload(field):
    with pytest.raises(ValidationError):
        create(local_install(), **{field: None})


def test_an_ordinary_task_cannot_carry_a_local_source():
    with pytest.raises(ValidationError):
        create(local_install(), type="feature")


def package_build(**changes) -> dict:
    target = local_install()
    return {
        key: target[key]
        for key in ("path", "package", "core_version", "python_version", "tooling_commit")
    } | changes


def feature(**changes) -> TaskCreate:
    return TaskCreate(
        **{
            "project_id": PROJECT,
            "title": "Generate notes digest",
            "type": "feature",
            "story_id": "story-1",
            "repository_id": "repo-1",
            "package_build": package_build(),
            **changes,
        }
    )


def test_a_build_intent_keeps_its_exact_identity_through_create_and_read():
    created = feature()
    assert created.model_dump(mode="json")["package_build"] == package_build()
    task = read(created)
    assert isinstance(task.package_build, LocalPackageBuild)
    assert task.package_build.model_dump(mode="json") == package_build()
    assert task.install is None


@pytest.mark.parametrize(
    "changes",
    [
        {"route": "package_source"},
        {"commit": "1" * 40},
        {"project_id": str(PROJECT)},
        {"repository_id": "repo-1"},
        {"catalog": catalog_install()["catalog"]},
        {"package": {**local_install()["package"], "tag": "packages/x/v0.1.0"}},
        {"wheel": "services/backend/packages/notes.whl"},
        {"command": "kit add"},
        {"proof": "built"},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_a_build_intent_names_no_commit_route_owner_release_or_proof(changes):
    with pytest.raises(ValidationError):
        LocalPackageBuild.model_validate(package_build(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "packages/../notes"},
        {"core_version": "2.x"},
        {"python_version": ""},
        {"tooling_commit": "main"},
    ],
    ids=["path", "core", "python", "tooling"],
)
def test_a_build_intent_uses_the_canonical_target_field_types(changes):
    with pytest.raises(ValidationError):
        LocalPackageBuild.model_validate(package_build(**changes))


def test_a_build_intent_and_an_install_target_never_validate_as_each_other():
    with pytest.raises(ValidationError):
        parse_install_target(package_build())
    with pytest.raises(ValidationError):
        LocalPackageBuild.model_validate(local_install())


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "create"},
        {"type": "fix"},
        {"type": "refactor"},
        {"type": "install", "install": local_install()},
    ],
    ids=["create", "fix", "refactor", "install"],
)
def test_only_a_feature_without_an_install_payload_carries_a_build_intent(changes):
    feature(package_build=None, **changes)
    with pytest.raises(ValidationError):
        feature(**changes)


@pytest.mark.parametrize("field", ["story_id", "repository_id"])
@pytest.mark.parametrize("value", [None, ""])
def test_a_build_intent_requires_story_and_repository_ownership(field, value):
    feature(package_build=None, **{field: value})
    with pytest.raises(ValidationError):
        feature(**{field: value})
