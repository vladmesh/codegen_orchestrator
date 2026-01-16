"""Tests for copier project generation from service-template."""

from pathlib import Path


class TestCopierGeneratesProject:
    """Test that copier generates a valid project structure."""

    def test_copier_generates_backend_project(self, generated_backend_project: Path) -> None:
        """Test that scaffolder generates a valid backend project.

        Verifies:
        - Basic project structure exists
        - Backend service directory created
        - No frontend/tg_bot directories (not requested)
        """
        project = generated_backend_project

        # Core files should exist
        assert (project / "Makefile").exists(), "Missing Makefile"
        assert (project / "README.md").exists(), "Missing README.md"
        assert (project / ".env.example").exists(), "Missing .env.example"
        assert (project / "pytest.ini").exists(), "Missing pytest.ini"
        assert (project / "ruff.toml").exists(), "Missing ruff.toml"

        # Backend service should exist
        assert (project / "services" / "backend").is_dir(), "Missing backend service"
        assert (
            project / "services" / "backend" / "Dockerfile"
        ).exists(), "Missing backend Dockerfile"

        # Other modules should NOT exist (not requested)
        assert not (project / "services" / "tg_bot").exists(), "tg_bot should not exist"
        assert not (project / "services" / "frontend").exists(), "frontend should not exist"

    def test_copier_generates_multi_module_project(
        self, generated_multi_module_project: Path
    ) -> None:
        """Test that scaffolder generates multiple modules correctly.

        Verifies:
        - Both backend and tg_bot directories exist
        - Each module has required files
        """
        project = generated_multi_module_project

        # Backend should exist
        assert (project / "services" / "backend").is_dir(), "Missing backend service"
        assert (
            project / "services" / "backend" / "Dockerfile"
        ).exists(), "Missing backend Dockerfile"

        # Telegram bot should exist
        assert (project / "services" / "tg_bot").is_dir(), "Missing tg_bot service"
        assert (
            project / "services" / "tg_bot" / "Dockerfile"
        ).exists(), "Missing tg_bot Dockerfile"

    def test_generated_project_has_required_files(self, generated_backend_project: Path) -> None:
        """Test that generated project has all required files for orchestrator.

        These files are expected by the orchestrator workflow.
        """
        project = generated_backend_project

        required_files = [
            "Makefile",
            "README.md",
            ".env.example",
            "pytest.ini",
            "ruff.toml",
            "mypy.ini",
            ".copier-answers.yml",
            "infra/compose.base.yml",
            ".github/workflows/main.yml",
        ]

        for file in required_files:
            path = project / file
            assert path.exists(), f"Missing required file: {file}"

    def test_generated_project_has_shared_directory(self, generated_backend_project: Path) -> None:
        """Test that generated project has shared directory structure."""
        project = generated_backend_project

        # Shared directory should exist
        assert (project / "shared").is_dir(), "Missing shared directory"

        # Important subdirectories
        expected_dirs = ["spec"]
        for d in expected_dirs:
            assert (project / "shared" / d).is_dir(), f"Missing shared/{d} directory"
