"""Fixtures for template integration tests."""

from collections.abc import Generator
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

# Default to GitHub URL - copier will clone it
DEFAULT_TEMPLATE_PATH = "gh:vladmesh/service-template"
TEMPLATE_PATH = os.getenv("SERVICE_TEMPLATE_PATH", DEFAULT_TEMPLATE_PATH)


@pytest.fixture
def tmp_output_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for copier output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def template_path() -> str:
    """Get the path to the service-template.

    Uses GitHub URL by default, can be overridden via env var.
    """
    return TEMPLATE_PATH


def run_copier(
    template: str,
    output_dir: Path,
    project_name: str = "test-project",
    modules: str = "backend",
    extra_data: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run copier with the given parameters.

    Args:
        template: Path or URL to the copier template
        output_dir: Directory to output the generated project
        project_name: Name for the project
        modules: Comma-separated list of modules
        extra_data: Additional data to pass to copier

    Returns:
        CompletedProcess from subprocess.run
    """
    data = {
        "project_name": project_name,
        "modules": modules,
        "project_description": "Test project for integration tests",
        "author_name": "Test Author",
        "author_email": "test@example.com",
    }
    if extra_data:
        data.update(extra_data)

    # Build copier command
    cmd = [
        "copier",
        "copy",
        "--trust",  # Skip confirmation prompts
        "--defaults",  # Use defaults for unspecified values
        "--vcs-ref=HEAD",  # Ensure we use the latest HEAD of the template
    ]

    # Add data arguments
    for key, value in data.items():
        cmd.extend(["--data", f"{key}={value}"])

    cmd.extend([template, str(output_dir)])

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,  # Don't raise on non-zero exit
    )


@pytest.fixture
def generated_backend_project(template_path: str, tmp_output_dir: Path) -> Path:
    """Generate a backend-only project and return its path."""
    result = run_copier(
        template=template_path,
        output_dir=tmp_output_dir,
        project_name="test-backend",
        modules="backend",
    )
    if result.returncode != 0:
        pytest.fail(f"Copier failed: {result.stderr}\n{result.stdout}")
    return tmp_output_dir


@pytest.fixture
def generated_multi_module_project(template_path: str, tmp_output_dir: Path) -> Path:
    """Generate a multi-module project (backend + tg_bot)."""
    result = run_copier(
        template=template_path,
        output_dir=tmp_output_dir,
        project_name="test-multi",
        modules="backend,tg_bot",
    )
    if result.returncode != 0:
        pytest.fail(f"Copier failed: {result.stderr}\n{result.stdout}")
    return tmp_output_dir
