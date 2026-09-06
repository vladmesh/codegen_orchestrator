"""Run active package revisions with exclusive PostgreSQL ownership."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path
import re

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text

from services.backend.src.core.settings import get_settings

from .packages import ActivatedPackage, discover_generated_packages

_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _migration_directory(declaration: str) -> Path:
    module, separator, relative = declaration.partition(":")
    if not separator or not module or not relative:
        raise RuntimeError(
            f"package migration resource {declaration!r} must use module:path syntax"
        )
    target = resources.files(module).joinpath(relative)
    if not target.is_dir():
        raise RuntimeError(f"package migration resource {declaration!r} is not a directory")
    return Path(str(target))


def _upgrade(connection: Connection, package: ActivatedPackage) -> None:
    """Apply one package only inside its schema and schema-local version table."""

    schema = package.manifest.database_schema
    declaration = package.manifest.database_migrations
    if schema is None or declaration is None:
        return
    if schema == "public" or _SCHEMA.fullmatch(schema) is None:
        raise RuntimeError(f"package {package.manifest.name!r} has invalid database schema")
    quoted = connection.dialect.identifier_preparer.quote(schema)
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quoted}"))
    connection.execute(text(f"SET LOCAL search_path TO {quoted}, public"))

    config = Config()
    config.set_main_option("script_location", str(_migration_directory(declaration)))
    script = ScriptDirectory.from_config(config)

    def upgrade_revisions(revision: str, _context: object) -> Iterable[object]:
        return script._upgrade_revs("head", revision)  # noqa: SLF001

    with EnvironmentContext(
        config,
        script,
        fn=upgrade_revisions,
        destination_rev="head",
    ) as environment:
        environment.configure(
            connection=connection,
            version_table="alembic_version",
            version_table_schema=schema,
        )
        with environment.begin_transaction():
            environment.run_migrations()


def upgrade_packages(
    packages: Iterable[ActivatedPackage],
    *,
    connect: Callable[[], Connection],
) -> None:
    """Upgrade active packages in manifest order after core migration completes."""

    packages = tuple(packages)
    owners: dict[str, str] = {}
    for package in packages:
        schema = package.manifest.database_schema
        if schema is None:
            continue
        previous = owners.get(schema)
        if previous is not None:
            raise RuntimeError(
                f"database schema {schema!r} is declared by both {previous!r} "
                f"and {package.manifest.name!r}"
            )
        owners[schema] = package.manifest.name
    with connect() as connection:
        for package in packages:
            _upgrade(connection, package)
        connection.commit()


def main() -> None:
    """Resolve the generated active set and apply every package migration."""

    packages = discover_generated_packages()
    engine = create_engine(get_settings().sync_database_url)
    try:
        upgrade_packages(packages, connect=engine.connect)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
