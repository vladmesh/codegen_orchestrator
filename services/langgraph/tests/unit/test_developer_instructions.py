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
