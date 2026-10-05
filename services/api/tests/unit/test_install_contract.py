"""The catalog operation is typed data, never executable task prose."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.dto.task import TaskCreate
from shared.contracts.queues.scaffold import ScaffoldMessage


def install_payload():
    return {
        "package": {
            "name": "reminders",
            "distribution": "codegen-kit-reminders",
            "version": "0.5.0",
            "tag": "packages/reminders/v0.5.0",
        },
        "libraries": [
            {
                "name": "textparse",
                "distribution": "codegen-kit-textparse",
                "version": "0.1.0",
                "tag": "packages/textparse/v0.1.0",
            }
        ],
        "binding": {
            "package": "reminders",
            "resource": "codegen_kit_reminders:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": ["textparse.when"],
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "catalog_digest": "b" * 64,
        "tooling_commit": "c" * 40,
    }


def test_install_round_trip_requires_owned_typed_payload():
    task = TaskCreate(
        project_id=uuid.uuid4(),
        title="Install reminders",
        type="install",
        story_id="story-1",
        repository_id="repo-1",
        install=install_payload(),
    )
    assert task.model_dump(mode="json")["install"] == install_payload()


@pytest.mark.parametrize(
    "change",
    [
        {"install": None},
        {"repository_id": None},
        {"story_id": None},
        {"command": "kit add reminders"},
        {"install_operation": {"state": "published"}},
        {"install": {**install_payload(), "command": "sh -c anything"}},
        {
            "install": {
                **install_payload(),
                "package": {**install_payload()["package"], "name": "../reminders"},
            }
        },
    ],
)
def test_install_refuses_missing_ownership_and_arbitrary_execution(change):
    data = {
        "project_id": uuid.uuid4(),
        "title": "Install",
        "type": "install",
        "story_id": "story-1",
        "repository_id": "repo-1",
        "install": install_payload(),
    }
    with pytest.raises(ValidationError):
        TaskCreate(**{**data, **change})


def test_ordinary_task_cannot_hide_an_install():
    with pytest.raises(ValidationError):
        TaskCreate(project_id=uuid.uuid4(), title="Feature", install=install_payload())


@pytest.mark.parametrize(
    "field", ["task_id", "story_id", "operation_id", "cycle_started_at", "install", "repository_id"]
)
def test_install_message_requires_the_complete_owned_operation(field):
    data = {
        "project_id": "project-1",
        "repository_id": "repo-1",
        "template_repo": "gh:vladmesh/codegen-product-kit",
        "template_ref": TEMPLATE_PIN.ref,
        "project_name": "notes",
        "modules": "backend,tg_bot",
        "mode": "install",
        "task_id": "task-1",
        "story_id": "story-1",
        "operation_id": "operation-1",
        "cycle_started_at": datetime.now(UTC),
        "install": install_payload(),
    }
    assert ScaffoldMessage(**data).install.model_dump(mode="json") == install_payload()
    data.pop(field)
    with pytest.raises(ValidationError):
        ScaffoldMessage(**data)
