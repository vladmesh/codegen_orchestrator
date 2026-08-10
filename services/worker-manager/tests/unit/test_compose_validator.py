from pathlib import Path

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
            result = validate_command([cmd])
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

    @pytest.mark.parametrize(
        "args",
        [
            ["run", "--volume", "/:/host", "db"],
            ["run", "--volume=/:/host", "db"],
            ["run", "-v", "/:/host", "db"],
            ["run", "--cap-add", "SYS_ADMIN", "db"],
            ["run", "--cap-add=SYS_ADMIN", "db"],
            ["run", "--service-ports", "db"],
            ["run", "--env", "SECRET=value", "db"],
            ["run", "--name=orchestrator-db", "db"],
            ["run", "--user", "0", "db"],
        ],
    )
    def test_container_creating_runtime_scope_flags_are_rejected(self, args):
        result = validate_command(args)
        assert not result.valid, result.errors


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

    @pytest.mark.parametrize(
        "fragment",
        [
            "env_file: /etc/passwd",
            "extends: {file: /etc/compose.yml, service: app}",
            "build: {context: /etc}",
            "build: {context: ., dockerfile: /etc/Dockerfile}",
        ],
    )
    def test_source_only_workspace_escapes_are_rejected(self, tmp_path, fragment):
        source = tmp_path / "infra" / "compose.yml"
        source.parent.mkdir()
        source.write_text(f"services:\n  app:\n    image: alpine\n    {fragment}\n")

        result = validate_compose_file(source.read_text(), source_file=source, workspace_path=tmp_path)

        assert not result.valid, result.errors

    @pytest.mark.parametrize("kind", ["secrets", "configs"])
    def test_external_or_host_file_sources_are_rejected(self, tmp_path, kind):
        source = tmp_path / "compose.yml"
        source.write_text(
            f"services:\n  app:\n    image: alpine\n{kind}:\n  host:\n    external: true\n    file: /etc/passwd\n"
        )

        result = validate_compose_file(source.read_text(), source_file=source, workspace_path=tmp_path)

        assert not result.valid, result.errors

    def test_external_include_is_rejected_before_resolution(self, tmp_path):
        source = tmp_path / "compose.yml"
        source.write_text("include: /etc/compose.yml\nservices:\n  app:\n    image: alpine\n")

        result = validate_compose_file(source.read_text(), source_file=source, workspace_path=tmp_path)

        assert not result.valid, result.errors


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
                "networks": {"default": None},
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

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("network_mode", "container:codegen-postgres"),
            ("network_mode", "service:db"),
            ("network_mode", "bridge"),
            ("network_mode", "none"),
            ("pid", "container:codegen-postgres"),
            ("ipc", "shareable"),
        ],
    )
    def test_namespace_mode_bypasses_are_rejected(self, field, value):
        compose = _safe_effective_compose()
        compose["services"]["db"][field] = value

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid
        assert any(field in error for error in result.errors)

    def test_named_local_bind_volume_is_rejected(self):
        compose = _safe_effective_compose()
        compose["services"]["db"]["volumes"] = [{"type": "volume", "source": "hostroot", "target": "/host"}]
        compose["volumes"] = {
            "hostroot": {"driver": "local", "driver_opts": {"type": "none", "device": "/", "o": "bind"}}
        }

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid
        assert any("volume" in error.lower() for error in result.errors)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("uts", "host"),
            ("userns_mode", "host"),
            ("cgroup", "host"),
            ("volumes_from", ["container:orchestrator-db"]),
            ("security_opt", ["apparmor=unconfined"]),
        ],
    )
    def test_remaining_host_capable_service_fields_are_rejected(self, field, value):
        compose = _safe_effective_compose()
        compose["services"]["db"][field] = value

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid, result.errors
        assert any(field in error for error in result.errors)

    @pytest.mark.parametrize("definition", [{"external": True}, {"file": "/etc/passwd"}])
    def test_effective_secret_and_config_escapes_are_rejected(self, definition):
        compose = _safe_effective_compose()
        compose["secrets"] = {"host": definition}
        compose["configs"] = {"host": definition}

        result = validate_effective_compose(compose, "worker-123", Path("/workspace"))

        assert not result.valid, result.errors

    def test_external_named_volume_is_rejected(self):
        compose = _safe_effective_compose()
        compose["services"]["db"]["volumes"] = ["stolen:/data"]
        compose["volumes"] = {"stolen": {"external": True, "name": "orchestrator_data"}}

        result = validate_effective_compose(compose, "worker-123")

        assert not result.valid, result.errors

    def test_resolved_workspace_bind_is_allowed_but_external_bind_is_rejected(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        compose = _safe_effective_compose()
        compose["services"]["db"]["volumes"] = [{"type": "bind", "source": str(workspace / "data"), "target": "/data"}]

        safe_result = validate_effective_compose(compose, "worker-123", workspace)
        compose["services"]["db"]["volumes"][0]["source"] = "/etc"
        unsafe_result = validate_effective_compose(compose, "worker-123", workspace)

        assert safe_result.valid, safe_result.errors
        assert not unsafe_result.valid
        assert any("absolute bind" in error for error in unsafe_result.errors)

    def test_effective_relative_dockerfile_is_resolved_from_build_context(self, tmp_path):
        workspace = tmp_path / "workspace"
        build_context = workspace / "backend"
        build_context.mkdir(parents=True)
        compose = _safe_effective_compose()
        compose["services"]["db"]["build"] = {
            "context": str(build_context),
            "dockerfile": "Dockerfile",
        }

        result = validate_effective_compose(compose, "worker-123", workspace)

        assert result.valid, result.errors

        compose["services"]["db"]["build"]["dockerfile"] = "../../Dockerfile"
        escaped_result = validate_effective_compose(compose, "worker-123", workspace)

        assert not escaped_result.valid
        assert any("build.dockerfile" in error for error in escaped_result.errors)
