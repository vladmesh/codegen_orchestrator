"""Tests for generated GitHub Actions workflow validation."""

from pathlib import Path

import yaml


class TestWorkflowValidation:
    """Test that generated workflows are valid and meet orchestrator expectations."""

    def test_generated_ci_workflow_is_valid_yaml(self, generated_backend_project: Path) -> None:
        """Test that ci.yml is valid YAML."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "ci.yml"

        assert workflow_path.exists(), "ci.yml workflow not found"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        assert content is not None, "Empty workflow file"
        assert isinstance(content, dict), "Workflow should be a dict"

    def test_generated_deploy_workflow_is_valid_yaml(self, generated_backend_project: Path) -> None:
        """Test that deploy.yml is valid YAML."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "deploy.yml"

        assert workflow_path.exists(), "deploy.yml workflow not found"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        assert content is not None, "Empty workflow file"
        assert isinstance(content, dict), "Workflow should be a dict"

    def test_ci_workflow_has_expected_jobs(self, generated_backend_project: Path) -> None:
        """Test that ci.yml has the expected job structure."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "ci.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        assert "jobs" in content, "Missing 'jobs' section"
        jobs = content["jobs"]

        assert "lint-and-test" in jobs, "Missing 'lint-and-test' job"
        assert "build-and-push" in jobs, "Missing 'build-and-push' job"

    def test_ci_workflow_has_correct_triggers(self, generated_backend_project: Path) -> None:
        """Test that ci workflow has expected triggers.

        YAML parses 'on' as boolean True, so we check for both.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "ci.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        triggers = content.get("on") or content.get(True)
        assert triggers is not None, "Missing 'on' (triggers) section"

        assert "workflow_dispatch" in triggers, "Missing 'workflow_dispatch' trigger"
        assert "push" in triggers, "Missing 'push' trigger"

    def test_deploy_workflow_has_deploy_job(self, generated_backend_project: Path) -> None:
        """Test that deploy.yml has the deploy job."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "deploy.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        assert "jobs" in content, "Missing 'jobs' section"
        assert "deploy" in content["jobs"], "Missing 'deploy' job"

    def test_ci_workflow_uses_registry_secret(self, generated_backend_project: Path) -> None:
        """Test that CI workflow uses registry secrets for image push."""
        workflow_path = generated_backend_project / ".github" / "workflows" / "ci.yml"

        with open(workflow_path) as f:
            content = f.read()

        assert "secrets.REGISTRY_URL" in content, "Expected secrets.REGISTRY_URL in CI workflow"

    def test_multi_module_workflow_has_all_services(
        self, generated_multi_module_project: Path
    ) -> None:
        """Test that multi-module project workflow includes all services.

        When backend,tg_bot modules are requested, both should appear
        in the build matrix.
        """
        workflow_path = generated_multi_module_project / ".github" / "workflows" / "ci.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        build_job = content["jobs"].get("build-and-push", {})
        strategy = build_job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        includes = matrix.get("include", [])

        service_ids = [item.get("id") for item in includes if "id" in item]

        assert "backend" in service_ids, "Backend not in build matrix"
        assert "tg-bot" in service_ids, "tg-bot not in build matrix"
