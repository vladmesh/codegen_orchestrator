"""Tests for scaffold queue contract and constants."""

from datetime import UTC, datetime
import json

from pydantic import ValidationError
import pytest

from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.local_install import LocalPackageInstall
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import QUEUE_TOPOLOGY, SCAFFOLD_GROUP, SCAFFOLD_QUEUE
from shared.tests.unit.test_local_install import PROJECT, catalog_install, local_install


class TestScaffoldQueue:
    def test_topology_binding_exists(self):
        bindings = {b.stream: b.group for b in QUEUE_TOPOLOGY}
        assert bindings[SCAFFOLD_QUEUE] == SCAFFOLD_GROUP


class TestScaffoldMessage:
    def test_template_ref_is_required(self):
        with pytest.raises(ValidationError):
            ScaffoldMessage(
                project_id="p",
                repository_id="r",
                telegram_chat_id="u",
                template_repo="gh:vladmesh/service-template",
                project_name="project",
                modules="backend",
            )

    @pytest.mark.parametrize("template_ref", ["", "HEAD", "main", "bad ref", "--help"])
    def test_template_ref_rejects_floating_or_invalid_values(self, template_ref):
        with pytest.raises(ValidationError):
            ScaffoldMessage(
                project_id="p",
                repository_id="r",
                telegram_chat_id="u",
                template_repo="gh:vladmesh/service-template",
                template_ref=template_ref,
                project_name="project",
                modules="backend",
            )

    def test_admitted_template_sources_validate(self):
        """Both owner-controlled template repositories are admitted sources."""
        for source in ("gh:vladmesh/service-template", "gh:vladmesh/codegen-product-kit"):
            msg = ScaffoldMessage(
                project_id="p",
                repository_id="r",
                telegram_chat_id="u",
                template_repo=source,
                template_ref="0.3.0",
                project_name="project",
                modules="backend",
            )
            assert msg.template_repo == source

    def test_unlisted_template_source_is_refused(self):
        with pytest.raises(ValidationError):
            ScaffoldMessage(
                project_id="p",
                repository_id="r",
                telegram_chat_id="u",
                template_repo="gh:someone-else/service-template",
                template_ref="0.3.0",
                project_name="project",
                modules="backend",
            )

    def test_roundtrip(self):
        msg = ScaffoldMessage(
            project_id="proj-123",
            repository_id="repo-456",
            telegram_chat_id="user-1",
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="my-project",
            modules="backend,tg_bot",
        )
        data = msg.model_dump()
        restored = ScaffoldMessage.model_validate(data)
        assert restored.project_id == "proj-123"
        assert restored.repository_id == "repo-456"
        assert restored.template_repo == "gh:vladmesh/service-template"
        assert restored.template_ref == "0.3.0"
        assert restored.modules == "backend,tg_bot"

    def test_task_description_default_empty(self):
        msg = ScaffoldMessage(
            project_id="p",
            repository_id="r",
            telegram_chat_id="u",
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="n",
            modules="backend",
        )
        assert msg.task_description == ""

    def test_inherits_base_message_fields(self):
        msg = ScaffoldMessage(
            project_id="p",
            repository_id="r",
            telegram_chat_id="u",
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="n",
            modules="backend",
        )
        assert msg.version == "1"
        assert msg.request_id  # auto-generated UUID
        assert msg.correlation_id


def install_fields(target: dict, **changes) -> dict:
    return {
        "project_id": str(PROJECT),
        "repository_id": "repo-1",
        "template_repo": "gh:vladmesh/codegen-product-kit",
        "template_ref": "v1",
        "project_name": "notes",
        "modules": "backend,tg_bot",
        "mode": "install",
        "task_id": "task-1",
        "story_id": "story-1",
        "operation_id": "install-1",
        "cycle_started_at": "2026-01-01T00:00:00Z",
        "install": target,
        **changes,
    }


class TestInstallMessage:
    @pytest.mark.parametrize(
        ("target", "route"),
        [(catalog_install(), CatalogInstall), (local_install(), LocalPackageInstall)],
    )
    def test_both_target_routes_survive_the_wire_exactly(self, target, route):
        sent = ScaffoldMessage.model_validate(install_fields(target))
        wire = json.dumps(sent.model_dump(mode="json"))
        decoded = ScaffoldMessage.model_validate_json(wire)
        assert decoded == sent and isinstance(decoded.install, route)
        assert json.loads(wire)["install"] == target
        assert (decoded.task_id, decoded.story_id, decoded.operation_id) == (
            "task-1",
            "story-1",
            "install-1",
        )
        assert decoded.cycle_started_at == datetime(2026, 1, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        "target",
        [
            local_install(path="../outside"),
            local_install(commit="not-a-sha"),
            local_install(package={"name": "notes-digest", "version": "0.1.0"}),
            local_install(route="catalog"),
            local_install(catalog_digest="b" * 64),
            {**catalog_install(), "route": "package_source"},
            {key: value for key, value in local_install().items() if key != "route"},
        ],
    )
    def test_a_malformed_or_mixed_target_is_refused_at_decoding(self, target):
        with pytest.raises(ValidationError):
            ScaffoldMessage.model_validate(install_fields(target))

    @pytest.mark.parametrize(
        "changes",
        [
            {"project_id": "00000000-0000-0000-0000-0000000000b2"},
            {"repository_id": "repo-2"},
        ],
    )
    def test_a_local_target_of_another_product_is_refused_at_decoding(self, changes):
        with pytest.raises(ValidationError, match="names the message's project"):
            ScaffoldMessage.model_validate(install_fields(local_install(), **changes))

    @pytest.mark.parametrize(
        "missing", ["task_id", "story_id", "operation_id", "cycle_started_at", "install"]
    )
    def test_incomplete_install_ownership_is_refused(self, missing):
        with pytest.raises(ValidationError, match="install requires"):
            ScaffoldMessage.model_validate(install_fields(local_install(), **{missing: None}))

    @pytest.mark.parametrize("mode", ["full", "ensure"])
    def test_install_ownership_outside_install_mode_is_refused(self, mode):
        with pytest.raises(ValidationError, match="only valid for install mode"):
            ScaffoldMessage.model_validate(install_fields(local_install(), mode=mode))

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(ValidationError):
            ScaffoldMessage.model_validate(install_fields(local_install(), source_path="x"))
