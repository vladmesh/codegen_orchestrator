"""Unit tests for Story model — column types, defaults, FK."""

from shared.contracts.dto.story import StoryStatus
from shared.models.story import Story


class TestStoryModel:
    def test_tablename(self):
        assert Story.__tablename__ == "stories"

    def test_columns_exist(self):
        cols = {c.name for c in Story.__table__.columns}
        expected = {
            "id",
            "project_id",
            "parent_story_id",
            "title",
            "description",
            "acceptance_criteria",
            "status",
            "type",
            "priority",
            "blocked_by_story_id",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_type_default(self):
        col = Story.__table__.c.type
        assert col.default.arg == "product"

    def test_type_not_nullable(self):
        assert not Story.__table__.c.type.nullable

    def test_priority_default(self):
        col = Story.__table__.c.priority
        assert col.default.arg == 0

    def test_priority_not_nullable(self):
        assert not Story.__table__.c.priority.nullable

    def test_blocked_by_story_id_fk(self):
        col = Story.__table__.c.blocked_by_story_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "stories.id" in fk_targets

    def test_blocked_by_story_id_nullable(self):
        assert Story.__table__.c.blocked_by_story_id.nullable

    def test_project_id_fk(self):
        col = Story.__table__.c.project_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "projects.id" in fk_targets

    def test_project_id_not_nullable(self):
        assert not Story.__table__.c.project_id.nullable

    def test_parent_story_id_fk(self):
        col = Story.__table__.c.parent_story_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "stories.id" in fk_targets

    def test_parent_story_id_nullable(self):
        assert Story.__table__.c.parent_story_id.nullable

    def test_status_default(self):
        col = Story.__table__.c.status
        assert col.default.arg == StoryStatus.CREATED.value

    def test_created_by_default(self):
        col = Story.__table__.c.created_by
        assert col.default.arg == "system"

    def test_description_nullable(self):
        assert Story.__table__.c.description.nullable

    def test_acceptance_criteria_nullable(self):
        assert Story.__table__.c.acceptance_criteria.nullable

    def test_user_report_column_exists(self):
        cols = {c.name for c in Story.__table__.columns}
        assert "user_report" in cols

    def test_user_report_nullable(self):
        assert Story.__table__.c.user_report.nullable
