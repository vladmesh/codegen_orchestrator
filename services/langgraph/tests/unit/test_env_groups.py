"""Unit tests for env_groups module."""

from src.subgraphs.devops.env_groups import (
    GROUPS,
    PostgresGroup,
    RedisGroup,
    resolve_with_groups,
)


class TestPostgresGroup:
    """Tests for PostgresGroup."""

    def setup_method(self):
        self.group = PostgresGroup()

    def test_handles_returns_matching_vars(self):
        """handles() should return the intersection of input vars and supported vars."""
        variables = {"DATABASE_URL", "REDIS_URL", "POSTGRES_PASSWORD", "SECRET_KEY"}
        result = self.group.handles(variables)
        assert result == {"DATABASE_URL", "POSTGRES_PASSWORD"}

    def test_generates_consistent_password(self):
        """Password in DATABASE_URL must equal POSTGRES_PASSWORD."""
        variables = {"DATABASE_URL", "POSTGRES_PASSWORD"}
        result = self.group.resolve("my_project", variables)

        assert "DATABASE_URL" in result
        assert "POSTGRES_PASSWORD" in result

        # Extract password from DATABASE_URL: postgresql://postgres:<password>@postgres:5432/db_my_project
        url = result["DATABASE_URL"]
        password_in_url = url.split("://postgres:")[1].split("@")[0]
        assert password_in_url == result["POSTGRES_PASSWORD"]

    def test_handles_subset_only_database_url(self):
        """When only DATABASE_URL is requested, POSTGRES_PASSWORD is not generated."""
        variables = {"DATABASE_URL"}
        result = self.group.resolve("proj", variables)

        assert "DATABASE_URL" in result
        assert "POSTGRES_PASSWORD" not in result

    def test_async_database_url(self):
        """ASYNC_DATABASE_URL should use asyncpg scheme and share password with DATABASE_URL."""
        variables = {"DATABASE_URL", "ASYNC_DATABASE_URL", "POSTGRES_PASSWORD"}
        result = self.group.resolve("proj", variables)

        assert result["ASYNC_DATABASE_URL"].startswith("postgresql+asyncpg://")

        # Password must be consistent across all three
        sync_pass = result["DATABASE_URL"].split("://postgres:")[1].split("@")[0]
        async_pass = result["ASYNC_DATABASE_URL"].split("://postgres:")[1].split("@")[0]
        assert sync_pass == async_pass
        assert sync_pass == result["POSTGRES_PASSWORD"]

    def test_db_uses_project_id(self):
        """POSTGRES_DB should equal db_{project_id}."""
        variables = {"POSTGRES_DB", "DATABASE_URL"}
        result = self.group.resolve("my_project", variables)

        assert result["POSTGRES_DB"] == "db_my_project"
        assert result["DATABASE_URL"].endswith("/db_my_project")

    def test_postgres_user(self):
        """POSTGRES_USER should be 'postgres'."""
        variables = {"POSTGRES_USER"}
        result = self.group.resolve("proj", variables)
        assert result["POSTGRES_USER"] == "postgres"


class TestRedisGroup:
    """Tests for RedisGroup."""

    def test_handles_and_resolve(self):
        """RedisGroup should handle REDIS_URL and resolve to redis://redis:6379/0."""
        group = RedisGroup()
        variables = {"REDIS_URL", "DATABASE_URL", "SECRET_KEY"}

        assert group.handles(variables) == {"REDIS_URL"}

        result = group.resolve("proj", {"REDIS_URL"})
        assert result == {"REDIS_URL": "redis://redis:6379/0"}


class TestResolveWithGroups:
    """Tests for the resolve_with_groups function."""

    def test_returns_remaining(self):
        """Variables not handled by any group should be in 'remaining'."""
        variables = {"DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "REDIS_URL"}
        resolved, remaining = resolve_with_groups(variables, "proj")

        assert "DATABASE_URL" in resolved
        assert "REDIS_URL" in resolved
        assert remaining == {"SECRET_KEY", "JWT_SECRET"}

    def test_groups_do_not_overlap(self):
        """No variable should be handled by more than one group."""
        all_supported = []
        for group in GROUPS:
            all_supported.extend(group.SUPPORTED)

        # Check for duplicates
        assert len(all_supported) == len(set(all_supported)), (
            f"Overlapping variables: {[v for v in all_supported if all_supported.count(v) > 1]}"
        )
