"""Pinned toolchain versions for framework and generated-project CI."""

# Workflows repeat these literals; tooling tests enforce the exact pins.
# setup-uv v7.4.0 reads its bundled manifest, then falls back to this exact
# release URL for 0.11.29 instead of querying the mutable remote manifest.
SETUP_UV_ACTION = "astral-sh/setup-uv@6ee6290f1cbc4156c0bdd66691b2c144ef8df19a"

# Keep uv pinned exactly.
UV_VERSION = "0.11.29"
UV_LINUX_X86_64_CHECKSUM = "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829"
