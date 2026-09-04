"""Tests for developer worker INSTRUCTIONS.md content."""

from src.prompts import load_developer_instructions


class TestDeveloperInstructions:
    """Verify INSTRUCTIONS.md contains expected curl patterns and no CLI references."""

    def setup_method(self):
        self.content = load_developer_instructions()

    def test_loads_successfully(self):
        assert self.content, "INSTRUCTIONS.md should not be empty"

    def test_contains_result_reporting_endpoints(self):
        assert "localhost:9090/result" in self.content
        assert '"success":true' in self.content
        assert '"success":false' in self.content

    def test_contains_curl_commands(self):
        assert "curl -sf -X POST http://localhost:9090" in self.content

    def test_contains_infra_compose_proxy(self):
        assert "localhost:9090/infra/compose" in self.content

    def test_no_orchestrator_cli_references(self):
        assert "orchestrator dev-env" not in self.content
        assert "orchestrator project" not in self.content
        assert "orchestrator engineering" not in self.content
        assert "orchestrator deploy" not in self.content
        assert "orchestrator respond" not in self.content
        assert "orch reject" not in self.content
        assert "orch report-blocker" not in self.content

    def test_requires_env_contract_update_with_env_changes(self):
        assert "env.contract.yaml" in self.content
        assert "same commit" in self.content

    def test_uses_generated_project_test_commands(self):
        assert self.content.count("make tests") == 2
        assert "make test-integration" in self.content
        assert "make tests unit" not in self.content
        assert "make tests integration" not in self.content

    def test_requires_a_deployable_job_provider_before_reporting_success(self):
        lower = self.content.lower()
        assert "jobs_schema" in self.content
        assert 'provides: ["jobs.fire"]' in self.content
        assert "job_fired" in self.content
        assert "services.yml" in self.content
        assert "compose.base.yml" in self.content
        assert "compose.prod.yml" in self.content
        assert "durable output" in lower
        assert "dispatch_status" in self.content
        assert "notifications_worker" in self.content
        assert "Dockerfile" in self.content
        assert "env.contract.yaml" in self.content
        assert "CI build/push matrix" in self.content
        assert "docker compose -f infra/compose.prod.yml config" not in self.content
