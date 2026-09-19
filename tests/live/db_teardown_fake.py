"""A database that answers the four questions teardown asks, without a stack.

The offline live group drives `db_teardown` end to end against this: the
foreign-key catalog is the project's own schema (`metadata_catalog_payload`,
read off `shared.models`), the inventory answers with the keys a run owns, and
the residue pass answers with whatever the test says survived. So a test can
say "this run's deploy wrote a grant intent" or "one row was deliberately left"
and read back exactly what the stand would have been told.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace

from db_teardown import SqlResult

from shared.tests.project_cleanup import metadata_catalog_payload


class FakeDatabase:
    """Answers `db_teardown`'s queries in the order it asks them."""

    def __init__(
        self,
        *,
        catalog_payload: str | None = None,
        owned: Mapping[str, Sequence[str]] | None = None,
        residue: Sequence[tuple[str, str]] = (),
        delete_result: SqlResult | None = None,
    ) -> None:
        self.catalog_payload = (
            catalog_payload if catalog_payload is not None else metadata_catalog_payload()
        )
        self.owned = {table: list(keys) for table, keys in (owned or {}).items()}
        self.residue = list(residue)
        self.delete_result = delete_result
        self.queries: list[str] = []
        self.argv: list[list[str]] = []
        self.deleted = False

    # ── the boundary ────────────────────────────────────────────────────

    def run_sql(self, sql: str) -> SqlResult:
        self.queries.append(sql)
        if sql.startswith("SELECT json_build_object"):
            return SqlResult(returncode=0, stdout=self.catalog_payload + "\n", stderr="")
        if sql.startswith("BEGIN;"):
            self.deleted = True
            return self.delete_result or SqlResult(returncode=0, stdout="DELETE 1\n", stderr="")
        rows = self.residue if self.deleted else self._inventory_rows(sql)
        return SqlResult(
            returncode=0,
            stdout="".join(f"{table}\t{key}\n" for table, key in rows),
            stderr="",
        )

    def subprocess_run(self, argv, **kwargs):
        """Stand in for `subprocess.run` so `pipeline_helpers._psql` can be driven.

        The batch arrives the way psql is given it — on stdin, so that a test
        reads back what a real invocation would have sent.
        """
        self.argv.append(list(argv))
        result = self.run_sql(kwargs["input"])
        return SimpleNamespace(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    # ── what a test reads back ──────────────────────────────────────────

    def _inventory_rows(self, sql: str) -> list[tuple[str, str]]:
        return [
            (table, key)
            for table, keys in self.owned.items()
            if f"'{table}' AS tbl" in sql
            for key in keys
        ]

    @property
    def delete_sql(self) -> str:
        return next(sql for sql in self.queries if sql.startswith("BEGIN;"))

    @property
    def deleted_tables(self) -> list[str]:
        return [
            line.split()[2]
            for line in self.delete_sql.splitlines()
            if line.startswith("DELETE FROM ")
        ]
