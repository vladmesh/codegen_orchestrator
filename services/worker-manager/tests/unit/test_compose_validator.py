import pytest

from src.compose_validator import (
    validate_command,
    validate_compose_file,
    validate_effective_compose,
    resolve_compose_path,
    ALLOWED_COMMANDS,
)


class TestValidateCommand:
    def test_allowed_commands_pass(self):
        for cmd in ALLOWED_COMMANDS:
            result = validate_command([cmd, "-d"])
            assert result.valid, f"Expected '{cmd}' to be allowed, got errors: {result.errors}"

    def test_blocked_command_rejected(self):
        result = validate_command(["exec", "db", "bash"])
        assert not result.valid
        assert any("exec" in e for e in result.errors)

    def test_interactive_flags_blocked(self):
        result = validate_command(["run", "-it", "db"])
        assert not result.valid
        assert any("-it" in e for e in result.errors)

    def test_interactive_long_flag_blocked(self):
        result = validate_command(["run", "--interactive", "db"])
        assert not result.valid

    def test_no_subcommand_rejected(self):
        result = validate_command(["--verbose"])
        assert not result.valid

    def test_valid_up_with_options(self):
        result = validate_command(["up", "-d", "--wait", "db", "redis"])
        assert result.valid

    def test_file_flag_skipped_when_finding_subcommand(self):
        """The -f flag value should not be mistaken for a subcommand."""
        result = validate_command(["-f", "infra/compose.base.yml", "up", "-d", "db"])
        assert result.valid, f"Expected valid, got errors: {result.errors}"

    def test_multiple_file_flags(self):
        result = validate_command(["-f", "compose.yml", "-f", "compose.override.yml", "up", "-d"])
        assert result.valid

    def test_project_scope_overrides_are_rejected(self):
        for args in (
            ["--project-name", "another-project", "up"],
            ["--project-directory=/tmp", "up"],
            ["--env-file", "attacker.env", "up"],
            ["run", "--network", "codegen_internal", "db"],
            ["--file=outside.yml", "up"],
        ):
            result = validate_command(args)
            assert not result.valid, args


class TestValidateComposeFile:
    def test_relative_volume_allowed(self):
        content = """
services:
  db:
    image: postgres:16
    volumes:
      - ./data:/var/lib/postgresql/data
"""
        result = validate_compose_file(content)
        assert result.valid, result.errors

    def test_named_volume_allowed(self):
        content = """
services:
  db:
    image: postgres:16
    volumes:
      - db_data:/var/lib/postgresql/data
volumes:
  db_data:
"""
        result = validate_compose_file(content)
        assert result.valid, result.errors

    def test_absolute_volume_blocked(self):
        content = """
services:
  db:
    image: postgres:16
    volumes:
      - /etc/passwd:/etc/passwd
"""
        result = validate_compose_file(content)
        assert not result.valid
        assert any("absolute" in e for e in result.errors)

    def test_root_mount_blocked(self):
        content = """
services:
  app:
    image: alpine
    volumes:
      - /:/host
"""
        result = validate_compose_file(content)
        assert not result.valid

    def test_ports_allowed(self):
        """Ports are no longer blocked — conflicts are handled by docker compose naturally."""
        content = """
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
"""
        result = validate_compose_file(content)
        assert result.valid

    def test_invalid_yaml_error(self):
        result = validate_compose_file("not: valid: yaml: [\n")
        assert not result.valid
        assert any("YAML" in e or "yaml" in e.lower() for e in result.errors)

    def test_valid_minimal_compose(self):
        content = """
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: secret
"""
        result = validate_compose_file(content)
        assert result.valid, result.errors

    def test_absolute_bind_long_syntax_blocked(self):
        content = """
services:
  app:
    image: alpine
    volumes:
      - type: bind
        source: /etc
        target: /etc
"""
        result = validate_compose_file(content)
        assert not result.valid


class TestResolveComposePath:
    def test_valid_path_resolved(self, tmp_path):
        resolved, result = resolve_compose_path(".", tmp_path)
        assert result.valid
        assert resolved == tmp_path.resolve()

    def test_path_traversal_blocked(self, tmp_path):
        _, result = resolve_compose_path("../../etc", tmp_path)
        assert not result.valid
        assert any("traversal" in e.lower() for e in result.errors)

    def test_nested_subdir_allowed(self, tmp_path):
        subdir = tmp_path / "subproject"
        subdir.mkdir()
        resolved, result = resolve_compose_path("subproject", tmp_path)
        assert result.valid
        assert resolved == subdir.resolve()


def _safe_effective_compose():
    return {
        "services": {
            "db": {
                "image": "postgres:16",
                "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "512M"}}},
            }
        },
        "networks": {"default": {"name": "dev_proj_worker-123", "external": True}},
    }


class TestValidateEffectiveCompose:
    def test_safe_bounded_effective_compose_passes(self):
        result = validate_effective_compose(_safe_effective_compose(), "worker-123")
        assert result.valid, result.errors

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("privileged", True),
            ("network_mode", "host"),
            ("pid", "host"),
            ("ipc", "host"),
            ("devices", ["/dev/fuse"]),
            ("device_cgroup_rules", ["c 1:3 rwm"]),
            ("cap_add", ["SYS_ADMIN"]),
        ],
    )
    def test_host_escape_settings_are_rejected(self, field, value):
        compose = _safe_effective_compose()
        compose["services"]["db"][field] = value
        result = validate_effective_compose(compose, "worker-123")
        assert not result.valid
        assert any(field in error for error in result.errors)

    def test_socket_mount_custom_network_and_unbounded_resources_are_rejected(self):
        compose = _safe_effective_compose()
        service = compose["services"]["db"]
        service["volumes"] = ["./docker.sock:/var/run/docker.sock"]
        service["networks"] = ["orchestrator"]
        del service["deploy"]
        compose["networks"] = {"orchestrator": {"external": True}}

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid
        assert any("socket" in error.lower() for error in result.errors)
        assert any("network" in error.lower() for error in result.errors)
        assert any("limits" in error.lower() for error in result.errors)

    def test_over_limit_resources_are_rejected(self):
        compose = _safe_effective_compose()
        compose["services"]["db"]["deploy"]["resources"]["limits"] = {"cpus": "4.1", "memory": "5G"}

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid
        assert any("CPU" in error for error in result.errors)
        assert any("memory" in error.lower() for error in result.errors)
