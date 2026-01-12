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

    def test_dockerfile_empty_capabilities_no_apt_install(self, builder):
        """No capabilities = no apt-get install."""
        dockerfile = builder.generate_dockerfile(capabilities=[])
        assert "apt-get install" not in dockerfile

    def test_dockerfile_git_capability_installs_git(self, builder):
        """GIT capability should install git package."""
        dockerfile = builder.generate_dockerfile(capabilities=["GIT"])
        assert "apt-get" in dockerfile
        assert "git" in dockerfile

    def test_dockerfile_github_cli_capability_installs_gh(self, builder):
        """GITHUB_CLI capability should install gh CLI."""
        dockerfile = builder.generate_dockerfile(capabilities=["GITHUB_CLI"])
        # gh CLI requires special installation (not just apt-get)
        assert "gh" in dockerfile.lower()

    def test_dockerfile_curl_capability_installs_curl(self, builder):
        """CURL capability should install curl."""
        dockerfile = builder.generate_dockerfile(capabilities=["CURL"])
        assert "curl" in dockerfile

    def test_dockerfile_docker_capability_note(self, builder):
        """DOCKER capability uses bind mount, not installation."""
        # Docker-in-Docker is handled at runtime (mount /var/run/docker.sock)
        # Dockerfile should add docker CLI only
        dockerfile = builder.generate_dockerfile(capabilities=["DOCKER"])
        assert "docker" in dockerfile.lower()

    def test_dockerfile_multiple_capabilities_combined(self, builder):
        """Multiple capabilities should be combined efficiently."""
        dockerfile = builder.generate_dockerfile(capabilities=["GIT", "CURL"])
        # Should have single apt-get install with multiple packages
        assert "git" in dockerfile
        assert "curl" in dockerfile

    def test_dockerfile_deduplicates_capabilities(self, builder):
        """Duplicate capabilities should not cause duplicate installs."""
        dockerfile = builder.generate_dockerfile(capabilities=["GIT", "GIT"])
        # Count occurrences of 'git' in apt-get line
        lines = [line for line in dockerfile.split("\n") if "apt-get" in line]
        if lines:
            apt_line = lines[0]
            # 'git' should appear only once in install command
            assert apt_line.count(" git") <= 1


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
