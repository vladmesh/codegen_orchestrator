"""
ImageBuilder - Dockerfile generation and image caching for worker containers.

Part of P1.5 Runtime Cache implementation.

Responsibilities:
- Generate Dockerfiles based on requested capabilities
- Compute deterministic hashes for image caching
- Provide image tags for cache lookup
"""

import hashlib


# Capability to installation commands mapping
# Each capability maps to a list of Dockerfile instructions
CAPABILITY_INSTALL_MAP: dict[str, list[str]] = {
    "GIT": [
        "RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*",
    ],
    "CURL": [
        "RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*",
    ],
    "GITHUB_CLI": [
        # GitHub CLI installation per official docs
        "RUN apt-get update && apt-get install -y --no-install-recommends curl gpg && \\",
        "    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \\",
        '    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \\',
        "    apt-get update && apt-get install -y --no-install-recommends gh && \\",
        "    rm -rf /var/lib/apt/lists/*",
    ],
    "DOCKER": [
        # Docker CLI only - socket is mounted at runtime
        "RUN apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*",
    ],
}

# Packages that can be combined in a single apt-get install
APT_PACKAGES: dict[str, str] = {
    "GIT": "git",
    "CURL": "curl",
}


def compute_image_hash(capabilities: list[str], agent_type: str = "claude") -> str:
    """
    Compute deterministic hash for a set of capabilities and agent type.

    Args:
        capabilities: List of capability names (e.g., ["GIT", "DOCKER"])
        agent_type: Type of agent ("claude" or "factory"). Defaults to "claude".

    Returns:
        12-character lowercase hex hash

    Note:
        - Capabilities are sorted and deduplicated for determinism
        - Same capabilities in different order produce same hash
        - Different agent types with same capabilities produce DIFFERENT hash
    """
    # Normalize: uppercase, deduplicate, sort
    normalized = sorted(set(cap.upper() for cap in capabilities))

    # Create canonical string representation
    # Include agent_type in the canonical string to ensure uniqueness per agent
    canonical = f"{agent_type.lower()}:" + ",".join(normalized)

    # Compute SHA256 and truncate to 12 chars (per spec)
    hash_full = hashlib.sha256(canonical.encode()).hexdigest()
    return hash_full[:12]


class ImageBuilder:
    """
    Generates Dockerfiles for worker containers based on capabilities.

    Usage:
        builder = ImageBuilder(base_image="worker-base:latest")
        dockerfile = builder.generate_dockerfile(capabilities=["GIT", "CURL"])
        tag = builder.get_image_tag(capabilities=["GIT", "CURL"], prefix="worker")
    """

    def __init__(self, base_image: str):
        """
        Initialize ImageBuilder.

        Args:
            base_image: Base Docker image to extend (e.g., "worker-base:latest")
        """
        self.base_image = base_image

    def generate_dockerfile(self, capabilities: list[str]) -> str:
        """
        Generate Dockerfile content for given capabilities.

        Args:
            capabilities: List of capabilities to install (e.g., ["GIT", "CURL"])

        Returns:
            Complete Dockerfile content as string
        """
        lines = [f"FROM {self.base_image}"]

        # Normalize capabilities
        caps = sorted(set(cap.upper() for cap in capabilities))

        if not caps:
            # No capabilities - return minimal Dockerfile
            return "\n".join(lines)

        # Optimization: combine simple apt packages into single RUN
        apt_packages = []
        complex_caps = []

        for cap in caps:
            if cap in APT_PACKAGES:
                apt_packages.append(APT_PACKAGES[cap])
            elif cap in CAPABILITY_INSTALL_MAP:
                complex_caps.append(cap)

        # Add combined apt-get for simple packages
        if apt_packages:
            packages_str = " ".join(sorted(apt_packages))
            lines.append("")
            lines.append(
                f"RUN apt-get update && apt-get install -y --no-install-recommends {packages_str} && rm -rf /var/lib/apt/lists/*"
            )

        # Add complex installations (GITHUB_CLI, DOCKER)
        for cap in complex_caps:
            if cap in CAPABILITY_INSTALL_MAP:
                lines.append("")
                lines.extend(CAPABILITY_INSTALL_MAP[cap])

        return "\n".join(lines)

    def get_image_tag(self, capabilities: list[str], prefix: str, agent_type: str = "claude") -> str:
        """
        Generate Docker image tag for given capabilities.

        Args:
            capabilities: List of capabilities
            prefix: Image name prefix (e.g., "worker" or "worker-test")
            agent_type: Type of agent ("claude" or "factory")

        Returns:
            Full image tag (e.g., "worker:a1b2c3d4e5f6")
        """
        cap_hash = compute_image_hash(capabilities, agent_type=agent_type)
        return f"{prefix}:{cap_hash}"
