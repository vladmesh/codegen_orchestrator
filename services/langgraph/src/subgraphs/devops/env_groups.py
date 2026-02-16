"""Environment variable groups for coherent secret generation.

Related variables (e.g. DATABASE_URL and POSTGRES_PASSWORD) must share the same
generated password. Groups resolve all their variables atomically from a single
random seed so that passwords are always consistent.
"""

from abc import ABC, abstractmethod
import secrets as secrets_module


class EnvGroup(ABC):
    """Base class for a group of related environment variables."""

    SUPPORTED: set[str] = set()

    def handles(self, variables: set[str]) -> set[str]:
        """Return the subset of *variables* this group can resolve."""
        return variables & self.SUPPORTED

    @abstractmethod
    def resolve(self, project_id: str, variables: set[str]) -> dict[str, str]:
        """Generate values for *variables* (must be a subset of SUPPORTED).

        Only requested variables appear in the returned dict.
        """


class PostgresGroup(EnvGroup):
    """Postgres-related variables that share a single generated password."""

    SUPPORTED = {
        "DATABASE_URL",
        "ASYNC_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
    }

    def resolve(self, project_id: str, variables: set[str]) -> dict[str, str]:
        password = secrets_module.token_urlsafe(16)
        user = "postgres"
        db_name = f"db_{project_id}"

        result: dict[str, str] = {}
        for var in variables:
            if var == "DATABASE_URL":
                result[var] = f"postgresql://{user}:{password}@postgres:5432/{db_name}"
            elif var == "ASYNC_DATABASE_URL":
                result[var] = f"postgresql+asyncpg://{user}:{password}@postgres:5432/{db_name}"
            elif var == "POSTGRES_PASSWORD":
                result[var] = password
            elif var == "POSTGRES_USER":
                result[var] = user
            elif var == "POSTGRES_DB":
                result[var] = db_name
        return result


class RedisGroup(EnvGroup):
    """Redis-related variables."""

    SUPPORTED = {"REDIS_URL"}

    def resolve(self, project_id: str, variables: set[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        if "REDIS_URL" in variables:
            result["REDIS_URL"] = "redis://redis:6379/0"
        return result


GROUPS: list[EnvGroup] = [PostgresGroup(), RedisGroup()]


def resolve_with_groups(variables: set[str], project_id: str) -> tuple[dict[str, str], set[str]]:
    """Resolve variables using groups, returning (resolved, remaining).

    Each group claims and resolves its supported variables. Variables not
    handled by any group are returned in *remaining* for per-variable fallback.
    """
    resolved: dict[str, str] = {}
    claimed: set[str] = set()

    for group in GROUPS:
        handled = group.handles(variables)
        if handled:
            resolved.update(group.resolve(project_id, handled))
            claimed |= handled

    remaining = variables - claimed
    return resolved, remaining
