"""Delete one live run's database rows, and prove their absence, from the catalog.

Teardown used to be a hand-written list of `DELETE` statements ordered by
somebody who remembered the schema at the time they wrote it. That list goes
stale silently, and it goes stale in the direction of leaving residue behind:
run 35441716423 ended `cleanup failed` because `users_grant_intents` — the table
the level-1 grant deploy writes — references `runs.id` and was in nobody's list,
so the project row could not be deleted and stayed on the stand.

Nothing here restates the schema. The plan is derived from `pg_constraint`:

**What belongs to the run is what the foreign keys say belongs to it.** Starting
at `projects WHERE id = <the run's project>`, the plan walks *incoming* foreign
keys — the rows that point at a row the run owns — and never outgoing ones, so a
project's owner, its server and every other row the project merely refers to are
outside the closure and are never touched.

**Only the edges the database would refuse are followed.** `confdeltype` says
what Postgres does when the parent goes: `CASCADE` removes the child itself,
`SET NULL`/`SET DEFAULT` unlink it, and only `NO ACTION`/`RESTRICT` refuse the
delete. The plan therefore contains exactly the tables that must be emptied
first, which is also exactly the set whose absence has to be proven afterwards —
the append-only `engineering_attempt_ledger`, which FKs runs with `SET NULL`, is
correctly absent from both.

**The order is the catalog's, not a person's.** Deletion is the reverse
topological order of that closure; a cycle between two tables is raised by name
rather than guessed at.

**The proof is the same plan read back.** `inventory_sql` records every key the
run owns *before* the deletes, and `residue_sql` asks for those exact keys again
afterwards. A row that survived is reported as its table, its key and the
constraint by which it belongs to the run — so the next table that starts
referencing a run is caught by name here, rather than by the next stand run
failing its teardown.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import heapq
import json
import re

# `pg_constraint.confdeltype`, as Postgres spells it. Only these two refuse a
# parent delete while a child row is still there; the rest resolve themselves,
# so a child reached only through them is neither deleted nor expected to be.
REFUSING_DELETE_RULES: Mapping[str, str] = {"a": "NO ACTION", "r": "RESTRICT"}

CATALOG_SQL = """
SELECT json_build_object(
  'foreign_keys', COALESCE((SELECT json_agg(fk) FROM (
      SELECT con.conname AS constraint_name,
             child.relname AS child_table,
             parent.relname AS parent_table,
             con.confdeltype AS delete_rule,
             (SELECT json_agg(att.attname ORDER BY col.ord)
                FROM unnest(con.conkey) WITH ORDINALITY AS col(attnum, ord)
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = col.attnum) AS child_columns,
             (SELECT json_agg(att.attname ORDER BY col.ord)
                FROM unnest(con.confkey) WITH ORDINALITY AS col(attnum, ord)
                JOIN pg_attribute att
                  ON att.attrelid = con.confrelid AND att.attnum = col.attnum) AS parent_columns
      FROM pg_constraint con
      JOIN pg_class child ON child.oid = con.conrelid
      JOIN pg_class parent ON parent.oid = con.confrelid
      JOIN pg_namespace ns ON ns.oid = child.relnamespace
      WHERE con.contype = 'f' AND ns.nspname = 'public'
  ) AS fk), '[]'::json),
  'primary_keys', COALESCE((SELECT json_agg(pk) FROM (
      SELECT rel.relname AS table_name,
             (SELECT json_agg(att.attname ORDER BY col.ord)
                FROM unnest(con.conkey) WITH ORDINALITY AS col(attnum, ord)
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = col.attnum) AS columns
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
      JOIN pg_namespace ns ON ns.oid = rel.relnamespace
      WHERE con.contype = 'p' AND ns.nspname = 'public'
  ) AS pk), '[]'::json)
);
""".strip()

# What psql prints when a delete is refused. Parsed so the failure names the
# table and the constraint instead of handing an operator a raw error string.
_FK_VIOLATION = re.compile(
    r'violates foreign key constraint "(?P<constraint>[^"]+)" on table "(?P<child>[^"]+)"'
)
_FK_DETAIL = re.compile(r"DETAIL:\s*(?P<detail>.+)")


class TeardownError(RuntimeError):
    """A run's database rows could not be deleted, or could not be proven absent."""


@dataclass(frozen=True)
class ForeignKey:
    """One foreign key as `pg_constraint` holds it."""

    constraint: str
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    delete_rule: str

    @property
    def refuses_parent_delete(self) -> bool:
        return self.delete_rule in REFUSING_DELETE_RULES

    def describe(self) -> str:
        child = ", ".join(self.child_columns)
        parent = ", ".join(self.parent_columns)
        return (
            f"{self.child_table}.{child} → {self.parent_table}.{parent} "
            f"({self.constraint}, ON DELETE {REFUSING_DELETE_RULES.get(self.delete_rule, '?')})"
        )


@dataclass(frozen=True)
class Catalog:
    """The foreign keys and primary keys of one schema."""

    foreign_keys: tuple[ForeignKey, ...]
    primary_keys: Mapping[str, tuple[str, ...]]

    def refusing_children(self, table: str) -> tuple[ForeignKey, ...]:
        """The foreign keys that would refuse a delete of a row in `table`.

        Self-references are left out: one `DELETE` empties the table's own rows
        in a single statement, and `NO ACTION` is checked when that statement
        ends, so a parent row and its children go together.
        """
        return tuple(
            fk
            for fk in self.foreign_keys
            if fk.parent_table == table and fk.refuses_parent_delete and fk.child_table != table
        )


@dataclass(frozen=True)
class PlanStep:
    """One table of the closure, with the predicate that selects the run's rows."""

    table: str
    # SQL naming one row of this table: its primary key, as text.
    key_expression: str
    key_name: str
    predicate: str
    # The edges by which this table's rows belong to the run. Empty for the root.
    via: tuple[ForeignKey, ...] = ()

    def belongs_because(self) -> str:
        if not self.via:
            return "the run owns it directly"
        return "; ".join(fk.describe() for fk in self.via)


@dataclass
class TeardownReport:
    """What one teardown pass deleted, in the words its caller reports in."""

    project_id: str
    tables: list[str] = field(default_factory=list)
    owned_keys: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "tables": list(self.tables),
            "owned_keys": {table: list(keys) for table, keys in sorted(self.owned_keys.items())},
        }


@dataclass(frozen=True)
class SqlResult:
    """The outcome of one psql invocation, as the caller's runner reports it."""

    returncode: int
    stdout: str
    stderr: str


RunSql = Callable[[str], SqlResult]


def sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def parse_catalog(payload: str) -> Catalog:
    """Read the catalog query's JSON into the objects the plan is built from."""
    text = payload.strip()
    if not text:
        raise TeardownError("the foreign-key catalog query returned nothing")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TeardownError(f"the foreign-key catalog is not readable: {exc}") from exc
    foreign_keys = tuple(
        ForeignKey(
            constraint=row["constraint_name"],
            child_table=row["child_table"],
            child_columns=tuple(row["child_columns"]),
            parent_table=row["parent_table"],
            parent_columns=tuple(row["parent_columns"]),
            delete_rule=row["delete_rule"],
        )
        for row in data["foreign_keys"]
    )
    primary_keys = {row["table_name"]: tuple(row["columns"]) for row in data["primary_keys"]}
    return Catalog(foreign_keys=foreign_keys, primary_keys=primary_keys)


# What separates the columns of a composite key when it is printed and asked
# for as one value. Primary-key columns are NOT NULL, so `concat_ws` never
# collapses one away.
COMPOSITE_KEY_SEPARATOR = "|"


def _key_expression(catalog: Catalog, table: str) -> tuple[str, str]:
    """How a row of `table` is named, as SQL and as prose.

    Residue is reported by key, so every table in the closure needs one. Most
    have a single `id`; `analytics_known_users` is keyed by a pair, and a pair
    is read back as one value rather than being refused.
    """
    columns = catalog.primary_keys.get(table)
    if not columns:
        raise TeardownError(f"table {table!r} is in the teardown closure and has no primary key")
    if len(columns) == 1:
        return f"{columns[0]}::text", columns[0]
    joined = ", ".join(f"{column}::text" for column in columns)
    return (
        f"concat_ws({sql_literal(COMPOSITE_KEY_SEPARATOR)}, {joined})",
        COMPOSITE_KEY_SEPARATOR.join(columns),
    )


def _edge_predicate(fk: ForeignKey, parent_predicate: str) -> str:
    if len(fk.child_columns) != 1 or len(fk.parent_columns) != 1:
        raise TeardownError(
            f"constraint {fk.constraint!r} on {fk.child_table!r} spans several columns; "
            "teardown selects a run's rows one key at a time"
        )
    return (
        f"{fk.child_columns[0]} IN "
        f"(SELECT {fk.parent_columns[0]} FROM {fk.parent_table} WHERE {parent_predicate})"
    )


def _reachable(catalog: Catalog, root_table: str) -> dict[str, tuple[ForeignKey, ...]]:
    """Every table the root's rows are referenced from, with the edges that reach it."""
    incoming: dict[str, list[ForeignKey]] = {root_table: []}
    queue = [root_table]
    while queue:
        table = queue.pop(0)
        for fk in catalog.refusing_children(table):
            if fk.child_table not in incoming:
                incoming[fk.child_table] = []
                queue.append(fk.child_table)
            if fk not in incoming[fk.child_table]:
                incoming[fk.child_table].append(fk)
    return {table: tuple(edges) for table, edges in incoming.items()}


def _deletion_order(
    incoming: Mapping[str, tuple[ForeignKey, ...]], root_table: str
) -> tuple[str, ...]:
    """Children before parents, ties broken by name so the plan is reproducible."""
    dependents: dict[str, int] = dict.fromkeys(incoming, 0)
    for edges in incoming.values():
        for fk in edges:
            if fk.parent_table in dependents:
                dependents[fk.parent_table] += 1
    ready = [table for table, count in dependents.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        table = heapq.heappop(ready)
        order.append(table)
        for fk in incoming[table]:
            if fk.parent_table not in dependents:
                continue
            dependents[fk.parent_table] -= 1
            if dependents[fk.parent_table] == 0:
                heapq.heappush(ready, fk.parent_table)
    if len(order) != len(incoming):
        stuck = sorted(set(incoming) - set(order))
        raise TeardownError(
            "the foreign keys of these tables form a cycle, so no deletion order exists: "
            + ", ".join(stuck)
        )
    if order[-1] != root_table:
        raise TeardownError(f"deletion order ends at {order[-1]!r} rather than {root_table!r}")
    return tuple(order)


def build_plan(catalog: Catalog, *, root_table: str, root_predicate: str) -> tuple[PlanStep, ...]:
    """The ordered closure of rows the run owns, derived from the catalog alone."""
    incoming = _reachable(catalog, root_table)
    order = _deletion_order(incoming, root_table)
    predicates: dict[str, str] = {root_table: root_predicate}
    # Parents first, so a child's predicate can quote the predicate that selects
    # the parent rows it hangs off.
    for table in reversed(order):
        if table == root_table:
            continue
        predicates[table] = " OR ".join(
            _edge_predicate(fk, predicates[fk.parent_table]) for fk in incoming[table]
        )
    steps = []
    for table in order:
        expression, name = _key_expression(catalog, table)
        steps.append(
            PlanStep(
                table=table,
                key_expression=expression,
                key_name=name,
                predicate=predicates[table],
                via=incoming[table],
            )
        )
    return tuple(steps)


def delete_sql(plan: Sequence[PlanStep]) -> str:
    """One transaction: either the whole closure goes, or nothing does."""
    statements = [f"DELETE FROM {step.table} WHERE {step.predicate};" for step in plan]
    return "BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;"


def inventory_sql(plan: Sequence[PlanStep]) -> str:
    """Every key the run owns, read before the deletes so it can be asked for after."""
    selects = [
        f"SELECT {sql_literal(step.table)} AS tbl, {step.key_expression} AS rowkey "
        f"FROM {step.table} WHERE {step.predicate}"
        for step in plan
    ]
    return "\nUNION ALL\n".join(selects) + ";"


def residue_sql(plan: Sequence[PlanStep], owned: Mapping[str, Sequence[str]]) -> str:
    """Ask for exactly the keys the run owned. Anything answered is residue."""
    selects = []
    for step in plan:
        keys = owned.get(step.table) or ()
        if not keys:
            continue
        values = ", ".join(sql_literal(key) for key in keys)
        selects.append(
            f"SELECT {sql_literal(step.table)} AS tbl, {step.key_expression} AS rowkey "
            f"FROM {step.table} WHERE {step.key_expression} IN ({values})"
        )
    if not selects:
        return ""
    return "\nUNION ALL\n".join(selects) + ";"


def parse_rows(stdout: str) -> list[tuple[str, str]]:
    """`psql -t -A -F '\\t'` output as (table, key) pairs."""
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        table, _, key = line.partition("\t")
        rows.append((table.strip(), key.strip()))
    return rows


def group_rows(rows: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for table, key in rows:
        grouped.setdefault(table, []).append(key)
    return grouped


def describe_failure(stderr: str, plan: Sequence[PlanStep], catalog: Catalog) -> str:
    """Name the table and the constraint a refused delete tripped over.

    A bare psql string says which constraint refused; what an operator needs to
    know is whether the plan knew about that table at all, because that is the
    difference between a teardown that raced something and a catalog the plan
    was not built from.
    """
    text = stderr.strip()
    match = _FK_VIOLATION.search(text)
    if not match:
        return f"teardown SQL failed: {text}"
    constraint = match.group("constraint")
    child = match.group("child")
    edge = next((fk for fk in catalog.foreign_keys if fk.constraint == constraint), None)
    planned = {step.table for step in plan}
    where = (
        f"{child} is in the teardown plan, so its rows were written after the plan was built"
        if child in planned
        else f"{child} is not in the teardown plan, so the catalog it was built from is stale"
    )
    detail = _FK_DETAIL.search(text)
    parts = [
        f"teardown was refused by {constraint} on table {child}",
        edge.describe() if edge else f"constraint {constraint} is not in the catalog",
        where,
    ]
    if detail:
        parts.append(detail.group("detail").strip())
    return "; ".join(parts)


def format_residue(grouped: Mapping[str, Sequence[str]], plan: Sequence[PlanStep]) -> str:
    """Residue, named: table, keys, and the constraint that ties them to the run."""
    steps = {step.table: step for step in plan}
    lines = []
    for table in sorted(grouped):
        keys = ", ".join(sorted(grouped[table]))
        step = steps.get(table)
        because = step.belongs_because() if step else "no plan step names this table"
        named = f"{step.key_name}={keys}" if step else keys
        lines.append(f"{table}: {named} [{because}]")
    return "; ".join(lines)


def _require(result: SqlResult, what: str) -> str:
    if result.returncode != 0:
        raise TeardownError(f"{what}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def load_catalog(run_sql: RunSql) -> Catalog:
    return parse_catalog(_require(run_sql(CATALOG_SQL), "reading the foreign-key catalog"))


def teardown_project(project_id: str, run_sql: RunSql) -> TeardownReport:
    """Delete one project's rows and prove that none of them survived.

    Raises `TeardownError` — naming the table and the constraint — rather than
    reporting a clean teardown it cannot demonstrate.
    """
    catalog = load_catalog(run_sql)
    plan = build_plan(
        catalog,
        root_table="projects",
        root_predicate=f"id::text = {sql_literal(project_id)}",
    )
    owned = group_rows(
        parse_rows(_require(run_sql(inventory_sql(plan)), "reading the rows this run owns"))
    )
    deletion = run_sql(delete_sql(plan))
    if deletion.returncode != 0:
        raise TeardownError(describe_failure(deletion.stderr, plan, catalog))
    residue_query = residue_sql(plan, owned)
    if residue_query:
        left = group_rows(
            parse_rows(_require(run_sql(residue_query), "proving this run's rows are gone"))
        )
        if left:
            raise TeardownError(
                "database rows of this run survived teardown: " + format_residue(left, plan)
            )
    return TeardownReport(
        project_id=project_id,
        tables=[step.table for step in plan],
        owned_keys=owned,
    )
