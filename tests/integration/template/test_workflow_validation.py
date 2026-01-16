"""Tests for generated GitHub Actions workflow validation."""

from pathlib import Path

import yaml


class TestWorkflowValidation:
    """Test that generated workflows are valid and meet orchestrator expectations."""

    def test_generated_workflow_is_valid_yaml(self, generated_backend_project: Path) -> None:
        """Test that main.yml is valid YAML.

        Verifies:
        - File can be parsed as YAML
        - No syntax errors
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        assert workflow_path.exists(), "main.yml workflow not found"

        # Should parse without errors
        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        assert content is not None, "Empty workflow file"
        assert isinstance(content, dict), "Workflow should be a dict"

    def test_workflow_has_expected_jobs(self, generated_backend_project: Path) -> None:
        """Test that main.yml has the expected job structure.

        The orchestrator expects these jobs to exist in the workflow.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        # Should have jobs section
        assert "jobs" in content, "Missing 'jobs' section"
        jobs = content["jobs"]

        # Expected jobs
        assert "build-and-push" in jobs, "Missing 'build-and-push' job"
        assert "deploy" in jobs, "Missing 'deploy' job"

    def test_workflow_has_correct_triggers(self, generated_backend_project: Path) -> None:
        """Test that workflow has expected triggers.

        The orchestrator may trigger workflows via workflow_dispatch.
        Note: YAML parses 'on' as boolean True, so we check for both.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        # YAML parses 'on' as boolean True, so check for both
        triggers = content.get("on") or content.get(True)
        assert triggers is not None, "Missing 'on' (triggers) section"

        # Should support workflow_dispatch (for manual/orchestrator triggers)
        assert "workflow_dispatch" in triggers, "Missing 'workflow_dispatch' trigger"

        # Should also support push to main
        assert "push" in triggers, "Missing 'push' trigger"

    def test_workflow_uses_ghcr_registry(self, generated_backend_project: Path) -> None:
        """Test that workflow uses GitHub Container Registry.

        The orchestrator expects images to be pushed to ghcr.io.
        """
        workflow_path = generated_backend_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        # Check env section for REGISTRY
        if "env" in content:
            assert content["env"].get("REGISTRY") == "ghcr.io", "Expected REGISTRY=ghcr.io"

    def test_multi_module_workflow_has_all_services(
        self, generated_multi_module_project: Path
    ) -> None:
        """Test that multi-module project workflow includes all services.

        When backend,tg_bot modules are requested, both should appear
        in the build matrix.
        """
        workflow_path = generated_multi_module_project / ".github" / "workflows" / "main.yml"

        with open(workflow_path) as f:
            content = yaml.safe_load(f)

        # Get build-and-push job
        build_job = content["jobs"].get("build-and-push", {})
        strategy = build_job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        includes = matrix.get("include", [])

        # Extract service IDs from matrix
        service_ids = [item.get("id") for item in includes if "id" in item]

        assert "backend" in service_ids, "Backend not in build matrix"
        assert "tg-bot" in service_ids, "tg-bot not in build matrix"
