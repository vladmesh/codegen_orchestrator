"""Tests for spec_extractor module."""

import pytest

from src.spec_extractor import extract_specs_summary


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal scaffolded workspace with spec files."""
    # shared/spec/models.yaml
    models_dir = tmp_path / "shared" / "spec"
    models_dir.mkdir(parents=True)
    (models_dir / "models.yaml").write_text(
        """\
models:
  User:
    fields:
      id:
        type: int
        readonly: true
      name:
        type: string
        max_length: 100
      status:
        type:
          type: enum
          values: [active, banned]
          default: active
    variants:
      Create: {}
      Read: {}
  Post:
    fields:
      title:
        type: string
      author_id:
        type: int
"""
    )

    # shared/spec/events.yaml
    (models_dir / "events.yaml").write_text(
        """\
events:
  user_registered:
    message: User
    publish: true
    subscribe: false
  post_created:
    message: Post
    publish: true
    subscribe: true
"""
    )

    # services/backend/spec/users.yaml
    backend_spec = tmp_path / "services" / "backend" / "spec"
    backend_spec.mkdir(parents=True)
    (backend_spec / "users.yaml").write_text(
        """\
domain: users
config:
  rest:
    prefix: "/api/users"
    tags: ["users"]
operations:
  list_users:
    output: list[UserRead]
    rest:
      method: GET
      path: ""
  create_user:
    input: UserCreate
    output: UserRead
    rest:
      method: POST
      path: ""
      status: 201
    events:
      publish_on_success: user_registered
"""
    )

    # services/backend/spec/manifest.yaml (should be skipped)
    (backend_spec / "manifest.yaml").write_text("consumes: []\n")

    return tmp_path


class TestExtractSpecsSummary:
    def test_extracts_models(self, workspace):
        result = extract_specs_summary(workspace)

        assert "models" in result
        model_names = [m["name"] for m in result["models"]]
        assert "User" in model_names
        assert "Post" in model_names

        user = next(m for m in result["models"] if m["name"] == "User")
        assert "id" in user["fields"]
        assert "readonly" in user["fields"]["id"]
        assert "enum" in user["fields"]["status"]
        assert user["variants"] == ["Create", "Read"]

    def test_extracts_events(self, workspace):
        result = extract_specs_summary(workspace)

        assert "events" in result
        event_names = [e["name"] for e in result["events"]]
        assert "user_registered" in event_names
        assert "post_created" in event_names

        ur = next(e for e in result["events"] if e["name"] == "user_registered")
        assert ur["publish"] is True
        assert ur["subscribe"] is False

    def test_extracts_domains(self, workspace):
        result = extract_specs_summary(workspace)

        assert "domains" in result
        assert len(result["domains"]) == 1

        domain = result["domains"][0]
        assert domain["service"] == "backend"
        assert domain["domain"] == "users"
        assert domain["prefix"] == "/api/users"
        assert len(domain["operations"]) == 2

        create_op = next(o for o in domain["operations"] if o["name"] == "create_user")
        assert create_op["method"] == "POST"
        assert create_op["input"] == "UserCreate"
        assert create_op["publishes"] == "user_registered"

    def test_skips_manifest(self, workspace):
        """manifest.yaml should not be parsed as a domain spec."""
        result = extract_specs_summary(workspace)

        domain_names = [d["domain"] for d in result.get("domains", [])]
        assert "manifest" not in domain_names

    def test_empty_workspace(self, tmp_path):
        result = extract_specs_summary(tmp_path)
        assert result == {}

    def test_missing_models(self, workspace):
        (workspace / "shared" / "spec" / "models.yaml").unlink()
        result = extract_specs_summary(workspace)

        assert "models" not in result
        assert "events" in result  # events still there
        assert "domains" in result
