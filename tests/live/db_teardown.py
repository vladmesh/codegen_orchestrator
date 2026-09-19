"""Delete one live run's database rows, and prove their absence, from the catalog.

Teardown used to be a hand-written list of `DELETE` statements ordered by
somebody who remembered the schema at the time they wrote it. That list goes
stale silently, and it goes stale in the direction of leaving residue behind:
run 35441716423 ended `cleanup failed` because `users_grant_intents` — the table
the level-1 grant deploy writes — references `runs.id` and was in nobody's list,
so the project row could not be deleted and stayed on the stand. The second such
list, the stand sweep's own, stranded run 35451082771 the same way three weeks
later; it is gone too, and both teardown paths now start here.

Nothing here restates the schema. The plan is derived from `pg_constraint`:

**What belongs to the run is what the foreign keys say belongs to it.** Starting
at `projects WHERE <the caller's predicate>` — one run's project id for a run's
own teardown, the contour's title prefixes for the stand sweep, and nothing else
differs between them — the plan walks *incoming* foreign keys, the rows that
point at a row the run owns, and never outgoing ones, so a project's owner, its
server and every other row the project merely refers to are outside the closure
and are never touched.

**Only the edges the database would refuse are followed.** `confdeltype` says
what Postgres does when the parent goes: `CASCADE` removes the child itself,
`SET NULL`/`SET DEFAULT` unlink it, and only `NO ACTION`/`RESTRICT` refuse the
delete. The plan therefore contains exactly the tables that must be emptied
first, which is also exactly the set whose absence has to be proven afterwards —
the append-only `engineering_attempt_ledger`, which FKs runs with `SET NULL`, is
correctly absent from both. A table reachable *only* through a `CASCADE` edge —
`projects -CASCADE-> X -NO ACTION-> Y` — is therefore not in the plan either;
today no such path exists, because every `CASCADE` in this schema points at
`servers`, outside the closure. If one appears, Postgres refuses the delete of
`X` and `describe_failure` names the constraint and says the plan did not know
about `Y`: the gap surfaces as a loud refusal, not as silence.

**A foreign key is the only thing the catalog can see, so the columns that are
not one are derived too.** `service_deployments.project_id` is denormalized from
the application and carries no foreign key on purpose, and its `application_id`
is nullable — so a row can name this run's project and be reachable through no
key at all. `denormalized_references` finds such columns without listing them:
a column name that some foreign key in this schema uses for a parent (here
`project_id`, which a dozen tables FK to `projects` with) identifies every
*other* column of that name that carries no key of its own. Those become
predicates and ordering edges exactly like a foreign key, so the plan covers
`service_deployments` and `api_keys` by their `project_id` too, and the next
denormalized column joins it by existing. A name that resolves to more than one
parent table inside the closure is raised rather than guessed at, and an
explicit key outranks an inferred one: a table the schema unlinks by its own
`SET NULL`/`CASCADE` key into the closure is left alone however its other
columns are named, which is what keeps the deliberately-retained
`engineering_budget_reservations` out of the plan.

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
  ) AS pk), '[]'::json),
  'columns', COALESCE((SELECT json_agg(cl) FROM (
      SELECT rel.relname AS table_name, att.attname AS column_name
      FROM pg_attribute att
      JOIN pg_class rel ON rel.oid = att.attrelid
      JOIN pg_namespace ns ON ns.oid = rel.relnamespace
      WHERE ns.nspname = 'public' AND rel.relkind = 'r'
        AND att.attnum > 0 AND NOT att.attisdropped
  ) AS cl), '[]'::json)
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

    @property
    def child_key(self) -> str:
        return self._single(self.child_columns)

    @property
    def parent_key(self) -> str:
        return self._single(self.parent_columns)

    def _single(self, columns: tuple[str, ...]) -> str:
        if len(columns) != 1:
            raise TeardownError(
                f"constraint {self.constraint!r} on {self.child_table!r} spans several columns; "
                "teardown selects a run's rows one key at a time"
            )
        return columns[0]

    def describe(self) -> str:
        child = ", ".join(self.child_columns)
        parent = ", ".join(self.parent_columns)
        return (
            f"{self.child_table}.{child} → {self.parent_table}.{parent} "
            f"({self.constraint}, ON DELETE {REFUSING_DELETE_RULES.get(self.delete_rule, '?')})"
        )


@dataclass(frozen=True)
class DenormalizedReference:
    """A column that names a parent row without a foreign key to hold it there.

    Not a lesser foreign key: the database enforces nothing here, which is why
    such a row can survive a teardown refusing nothing and naming nothing. It is
    derived, never listed — see `denormalized_references`.
    """

    child_table: str
    child_key: str
    parent_table: str
    parent_key: str

    def describe(self) -> str:
        return (
            f"{self.child_table}.{self.child_key} → {self.parent_table}.{self.parent_key} "
            "(no foreign key: a denormalized reference)"
        )


# A reference by which a row belongs to the run, enforced or not.
Reference = ForeignKey | DenormalizedReference


@dataclass(frozen=True)
class Catalog:
    """The foreign keys, primary keys and columns of one schema."""

    foreign_keys: tuple[ForeignKey, ...]
    primary_keys: Mapping[str, tuple[str, ...]]
    columns: Mapping[str, tuple[str, ...]]

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
    # The references by which this table's rows belong to the run, foreign key
    # or denormalized column. Empty for the root.
    via: tuple[Reference, ...] = ()

    def belongs_because(self) -> str:
        if not self.via:
            return "the run owns it directly"
        return "; ".join(reference.describe() for reference in self.via)


@dataclass
class TeardownReport:
    """What one teardown pass deleted, in the words its caller reports in.

    `selection` is the predicate's subject: one run's project id for a per-run
    teardown, and the sweep's own description of the projects it selected.
    """

    selection: str
    tables: list[str] = field(default_factory=list)
    owned_keys: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "selection": self.selection,
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
    columns: dict[str, list[str]] = {}
    for row in data["columns"]:
        columns.setdefault(row["table_name"], []).append(row["column_name"])
    return Catalog(
        foreign_keys=foreign_keys,
        primary_keys=primary_keys,
        columns={table: tuple(names) for table, names in columns.items()},
    )


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


def _edge_predicate(reference: Reference, parent_predicate: str) -> str:
    return (
        f"{reference.child_key} IN "
        f"(SELECT {reference.parent_key} FROM {reference.parent_table} "
        f"WHERE {parent_predicate})"
    )


def denormalized_references(catalog: Catalog) -> tuple[DenormalizedReference, ...]:
    """Columns that name a parent row and carry no foreign key, derived.

    The schema's own foreign keys say what a column name means: `project_id` is
    a name a dozen tables use for `projects.id`. So a column of that name that
    is part of no foreign key on its own table is a reference the catalog walk
    would otherwise miss — `service_deployments.project_id`, denormalized from
    the application, and `api_keys.project_id` are the two this schema has.

    Nothing is listed here, so a new denormalized column is covered by existing.
    A name several tables use for *different* parents cannot be resolved this
    way; it is raised, by `build_plan`, if it touches the closure at all.
    """
    parents: dict[str, set[tuple[str, str]]] = {}
    covered: set[tuple[str, str]] = set()
    for fk in catalog.foreign_keys:
        for child_column, parent_column in zip(fk.child_columns, fk.parent_columns, strict=True):
            parents.setdefault(child_column, set()).add((fk.parent_table, parent_column))
            covered.add((fk.child_table, child_column))
    references = []
    for table, columns in sorted(catalog.columns.items()):
        for column in columns:
            if (table, column) in covered or column not in parents:
                continue
            for parent_table, parent_column in sorted(parents[column]):
                references.append(
                    DenormalizedReference(
                        child_table=table,
                        child_key=column,
                        parent_table=parent_table,
                        parent_key=parent_column,
                    )
                )
    return tuple(references)


def _ambiguous_names(references: Sequence[DenormalizedReference], tables: Iterable[str]) -> str:
    """Denormalized column names that resolve to more than one parent in the closure."""
    inside = set(tables)
    by_column: dict[tuple[str, str], set[str]] = {}
    for reference in references:
        if reference.parent_table in inside:
            by_column.setdefault((reference.child_table, reference.child_key), set()).add(
                reference.parent_table
            )
    ambiguous = {
        f"{table}.{column} → {', '.join(sorted(found))}"
        for (table, column), found in by_column.items()
        if len(found) > 1
    }
    return ", ".join(sorted(ambiguous))


def _unlinking_tables(catalog: Catalog, tables: Iterable[str]) -> set[str]:
    """Tables the schema already says what to do with when a closure row goes.

    A foreign key with `SET NULL` or `CASCADE` into the closure is the schema's
    own instruction for teardown, and it outranks a column name. That is what
    keeps `engineering_budget_reservations` — whose `project_id` is deliberately
    `ON DELETE SET NULL`, and whose `story_id` happens to carry no key — out of
    the plan, while `service_deployments`, whose only unlinking key points at
    `servers` outside the closure, stays in it.
    """
    inside = set(tables)
    return {
        fk.child_table
        for fk in catalog.foreign_keys
        if not fk.refuses_parent_delete
        and fk.parent_table in inside
        and fk.child_table != fk.parent_table
    }


def _walk(
    catalog: Catalog,
    root_table: str,
    denormalized: Sequence[DenormalizedReference],
    excluded: Iterable[str],
) -> dict[str, list[Reference]]:
    """One pass to a fixpoint over both kinds of reference."""
    refused = set(excluded)
    incoming: dict[str, list[Reference]] = {root_table: []}

    def reach(child: str, reference: Reference) -> bool:
        if child in refused:
            return False
        added = child not in incoming
        edges = incoming.setdefault(child, [])
        if reference not in edges:
            edges.append(reference)
        return added

    changed = True
    while changed:
        changed = False
        for table in list(incoming):
            for fk in catalog.refusing_children(table):
                changed |= reach(fk.child_table, fk)
        for reference in denormalized:
            if reference.parent_table in incoming and reference.child_table != root_table:
                changed |= reach(reference.child_table, reference)
    return incoming


def _reachable(catalog: Catalog, root_table: str) -> dict[str, tuple[Reference, ...]]:
    """Every table the root's rows are reached from, with the references that reach it.

    Two kinds of reference, one walk: a foreign key the database would refuse,
    and a denormalized column it would not. A denormalized table is a seed of
    its own, so the foreign keys pointing at *it* are followed too — which is
    why the walk runs to a fixpoint rather than in one pass.

    A table pulled in by a column name alone is dropped again when the schema
    has an unlinking key of its own into the closure, and the exclusion set only
    grows, so the outer loop terminates.
    """
    denormalized = denormalized_references(catalog)
    excluded: set[str] = set()
    while True:
        incoming = _walk(catalog, root_table, denormalized, excluded)
        by_name_only = {
            table
            for table, edges in incoming.items()
            if edges and all(isinstance(edge, DenormalizedReference) for edge in edges)
        }
        found = excluded | (by_name_only & _unlinking_tables(catalog, incoming))
        if found == excluded:
            break
        excluded = found
    ambiguous = _ambiguous_names(denormalized, incoming)
    if ambiguous:
        raise TeardownError(
            "these denormalized columns name more than one parent table, so teardown "
            "cannot tell which rows belong to the run: " + ambiguous
        )
    return {table: tuple(edges) for table, edges in incoming.items()}


def _deletion_order(
    incoming: Mapping[str, tuple[Reference, ...]], root_table: str
) -> tuple[str, ...]:
    """Children before parents, ties broken by name so the plan is reproducible."""
    dependents: dict[str, int] = dict.fromkeys(incoming, 0)
    for edges in incoming.values():
        for reference in edges:
            if reference.parent_table in dependents:
                dependents[reference.parent_table] += 1
    ready = [table for table, count in dependents.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        table = heapq.heappop(ready)
        order.append(table)
        for reference in incoming[table]:
            if reference.parent_table not in dependents:
                continue
            dependents[reference.parent_table] -= 1
            if dependents[reference.parent_table] == 0:
                heapq.heappush(ready, reference.parent_table)
    if len(order) != len(incoming):
        stuck = sorted(set(incoming) - set(order))
        raise TeardownError(
            "the references between these tables form a cycle, so no deletion order exists: "
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
            _edge_predicate(reference, predicates[reference.parent_table])
            for reference in incoming[table]
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


def teardown_selection(
    root_predicate: str, run_sql: RunSql, *, selection: str | None = None
) -> TeardownReport:
    """Delete the rows of every project a predicate selects, and prove they are gone.

    The predicate is the only thing a caller brings. One run's teardown names
    one project id; the stand sweep names the projects its contour's title
    prefixes match. Everything after that — which tables belong to the
    selection, in which order they go and which keys are read back — is derived
    from the catalog for both, so neither can go stale on its own schedule.

    Raises `TeardownError` — naming the table and the constraint — rather than
    reporting a clean teardown it cannot demonstrate.
    """
    catalog = load_catalog(run_sql)
    plan = build_plan(catalog, root_table="projects", root_predicate=root_predicate)
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
        selection=selection if selection is not None else root_predicate,
        tables=[step.table for step in plan],
        owned_keys=owned,
    )


def teardown_project(project_id: str, run_sql: RunSql) -> TeardownReport:
    """Delete one project's rows and prove that none of them survived."""
    return teardown_selection(
        f"id::text = {sql_literal(project_id)}", run_sql, selection=project_id
    )
