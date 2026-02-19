from src.compose_validator import (
    validate_command,
    validate_compose_file,
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

    def test_ports_blocked(self):
        content = """
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
"""
        result = validate_compose_file(content)
        assert not result.valid
        assert any("ports" in e for e in result.errors)

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
