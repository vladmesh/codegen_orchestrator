"""Test-only canonical project-deletion order assertions."""

import json
from typing import Final

PROJECT_BRIEF_TASK_STORY_DELETE_ORDER: Final[tuple[str, ...]] = (
    "requirement_coverages",
    "product_briefs",
    "tasks",
    "stories",
)

# `pg_constraint.confdeltype`, keyed by the `ondelete` SQLAlchemy carries. An
# unspecified rule is `NO ACTION`, which is what makes most of this schema's
# foreign keys refuse a parent delete.
_DELETE_RULES: Final[dict[str | None, str]] = {
    None: "a",
    "NO ACTION": "a",
    "RESTRICT": "r",
    "CASCADE": "c",
    "SET NULL": "n",
    "SET DEFAULT": "d",
}


def metadata_catalog_payload() -> str:
    """The live catalog query's answer, derived from this schema's own metadata.

    Offline teardown tests need the foreign keys the stand's database would
    report. Restating them by hand is the failure this exists to prevent, so
    they are read off `shared.models.Base.metadata` instead: a new table that
    references a run appears here the moment its model does, and a test that
    asserts the teardown plan covers it therefore tracks the schema rather than
    a copy of it. Unnamed constraints are spelled the way Postgres names them.
    """
    from shared.models import Base

    foreign_keys = []
    primary_keys = []
    for table in Base.metadata.sorted_tables:
        pk_columns = [column.name for column in table.primary_key.columns]
        if pk_columns:
            primary_keys.append({"table_name": table.name, "columns": pk_columns})
        for constraint in table.foreign_key_constraints:
            child_columns = [element.parent.name for element in constraint.elements]
            parent_columns = [element.column.name for element in constraint.elements]
            parent_table = constraint.elements[0].column.table.name
            name = constraint.name or f"{table.name}_{'_'.join(child_columns)}_fkey"
            foreign_keys.append(
                {
                    "constraint_name": name,
                    "child_table": table.name,
                    "parent_table": parent_table,
                    "delete_rule": _DELETE_RULES[constraint.ondelete],
                    "child_columns": child_columns,
                    "parent_columns": parent_columns,
                }
            )
    return json.dumps({"foreign_keys": foreign_keys, "primary_keys": primary_keys})
