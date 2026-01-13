"""
Unit tests for ImageBuilder (P1.5 Runtime Cache).

Tests cover:
- Dockerfile generation based on capabilities
- Deterministic hash calculation
- Correct package installation for each capability
"""

import pytest

# This import will fail initially (RED phase) - module doesn't exist yet
from src.image_builder import ImageBuilder, compute_image_hash


class TestComputeImageHash:
    """Test hash calculation for image caching."""

    def test_same_capabilities_different_order_produce_same_hash(self):
        """Capabilities order should not affect hash."""
        hash1 = compute_image_hash(["GIT", "DOCKER"])
        hash2 = compute_image_hash(["DOCKER", "GIT"])
        assert hash1 == hash2

    def test_different_capabilities_produce_different_hash(self):
        """Different capability sets must have different hashes."""
        hash1 = compute_image_hash(["GIT"])
        hash2 = compute_image_hash(["GIT", "DOCKER"])
        assert hash1 != hash2

    def test_compute_image_hash_determinism(self):
        """Hash should be deterministic for same capabilities."""
        caps1 = ["B", "A", "C"]
        caps2 = ["c", "b", "a"]  # Different order and case
        assert compute_image_hash(caps1) == compute_image_hash(caps2)

    def test_compute_image_hash_truncation(self):
        """Hash should be exactly 12 characters."""
        h = compute_image_hash(["A"])
        assert len(h) == 12

    def test_same_capabilities_different_agent_produce_different_hash(self):
        """Same capabilities but different agent = different image."""
        hash_claude = compute_image_hash(["GIT"], agent_type="claude")
        hash_factory = compute_image_hash(["GIT"], agent_type="factory")
        assert hash_claude != hash_factory

    def test_hash_deterministic_with_agent_type(self):
        """Agent type should be part of deterministic hash."""
        h1 = compute_image_hash(["GIT", "CURL"], agent_type="claude")
        h2 = compute_image_hash(["CURL", "GIT"], agent_type="claude")
        assert h1 == h2

    def test_empty_capabilities_has_consistent_hash(self):
        """Empty capabilities should produce consistent hash."""
        hash1 = compute_image_hash([])
        hash2 = compute_image_hash([])
        assert hash1 == hash2

    def test_hash_is_lowercase_hex(self):
        """Hash should be lowercase hexadecimal."""
        h = compute_image_hash(["GIT", "CURL"])
        assert h.isalnum()
        assert h == h.lower()


class TestImageBuilderDockerfileGeneration:
    """Test Dockerfile generation logic."""

    @pytest.fixture
    def builder(self):
        return ImageBuilder(base_image="worker-base:latest")

    def test_dockerfile_starts_with_from_base_image(self, builder):
        """Dockerfile must start with FROM base image."""
        dockerfile = builder.generate_dockerfile(capabilities=[])
        assert dockerfile.startswith("FROM worker-base:latest")

    def test_dockerfile_empty_capabilities_has_agent_label(self, builder):
        """Empty capabilities should still have agent type label."""
        dockerfile = builder.generate_dockerfile(capabilities=[], agent_type="claude")
        assert "LABEL" in dockerfile
        assert "claude" in dockerfile

    def test_dockerfile_git_capability_preinstalled(self, builder):
        """GIT is pre-installed in worker-base, no apt-get needed."""
        dockerfile = builder.generate_dockerfile(capabilities=["GIT"])
        # GIT is pre-installed, so no apt-get install for git
        # But LABEL should be present
        assert "LABEL" in dockerfile

    def test_dockerfile_github_cli_capability_installs_gh(self, builder):
        """GITHUB_CLI capability should install gh CLI."""
        dockerfile = builder.generate_dockerfile(capabilities=["GITHUB_CLI"])
        # gh CLI requires special installation
        assert "gh" in dockerfile.lower()
        assert "apt-get" in dockerfile

    def test_dockerfile_curl_capability_preinstalled(self, builder):
        """CURL is pre-installed in worker-base, no apt-get needed."""
        dockerfile = builder.generate_dockerfile(capabilities=["CURL"])
        # CURL is pre-installed
        assert "LABEL" in dockerfile

    def test_dockerfile_docker_capability_installs_docker_cli(self, builder):
        """DOCKER capability should install docker CLI."""
        dockerfile = builder.generate_dockerfile(capabilities=["DOCKER"])
        assert "docker" in dockerfile.lower()
        assert "apt-get" in dockerfile

    def test_dockerfile_preinstalled_capabilities_no_apt_install(self, builder):
        """Pre-installed capabilities (GIT, CURL) should not add apt-get."""
        dockerfile = builder.generate_dockerfile(capabilities=["GIT", "CURL"])
        # Only LABEL, no apt-get for pre-installed caps
        lines = [line for line in dockerfile.split("\n") if "apt-get install" in line]
        assert len(lines) == 0

    def test_dockerfile_agent_type_creates_unique_layer(self, builder):
        """Different agent types should produce different Dockerfiles."""
        dockerfile_claude = builder.generate_dockerfile(capabilities=["GIT"], agent_type="claude")
        dockerfile_factory = builder.generate_dockerfile(capabilities=["GIT"], agent_type="factory")
        # Different agent types should have different labels
        assert "claude" in dockerfile_claude
        assert "factory" in dockerfile_factory
        assert dockerfile_claude != dockerfile_factory


class TestImageBuilderImageTag:
    """Test image tag generation."""

    @pytest.fixture
    def builder(self):
        return ImageBuilder(base_image="worker-base:latest")

    def test_get_image_tag_includes_prefix(self, builder):
        """Image tag should use configured prefix."""
        tag = builder.get_image_tag(capabilities=["GIT"], prefix="worker")
        assert tag.startswith("worker:")

    def test_get_image_tag_includes_hash(self, builder):
        """Image tag should include capability hash."""
        tag = builder.get_image_tag(capabilities=["GIT"], prefix="worker")
        expected_hash = compute_image_hash(["GIT"])
        assert expected_hash in tag

    def test_get_image_tag_format(self, builder):
        """Image tag should be prefix:hash format."""
        tag = builder.get_image_tag(capabilities=["GIT", "CURL"], prefix="worker-test")
        parts = tag.split(":")
        assert len(parts) == 2
        assert parts[0] == "worker-test"
        assert len(parts[1]) == 12  # hash length
