"""Tests for scaffold queue contract and constants."""

from pydantic import ValidationError
import pytest

from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.queues import QUEUE_TOPOLOGY, SCAFFOLD_GROUP, SCAFFOLD_QUEUE


class TestScaffoldQueue:
    def test_queue_constant(self):
        assert SCAFFOLD_QUEUE == "scaffold:queue"

    def test_group_constant(self):
        assert SCAFFOLD_GROUP == "scaffold-consumers"

    def test_topology_binding_exists(self):
        bindings = {b.stream: b.group for b in QUEUE_TOPOLOGY}
        assert bindings[SCAFFOLD_QUEUE] == SCAFFOLD_GROUP


class TestScaffoldMessage:
    def test_template_ref_is_required(self):
        with pytest.raises(ValidationError):
            ScaffoldMessage(
                project_id="p",
                repository_id="r",
                user_id="u",
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
                user_id="u",
                template_repo="gh:vladmesh/service-template",
                template_ref=template_ref,
                project_name="project",
                modules="backend",
            )

    def test_roundtrip(self):
        msg = ScaffoldMessage(
            project_id="proj-123",
            repository_id="repo-456",
            user_id="user-1",
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
            user_id="u",
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
            user_id="u",
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="n",
            modules="backend",
        )
        assert msg.version == "1"
        assert msg.request_id  # auto-generated UUID
        assert msg.correlation_id
