"""`make lint`: unit tests run code; they do not read documentation or their own source.

A test under a `tests/unit` directory fails this check when it

- names a path in the repository's `docs/` tree (a string `docs` or `docs/...`) as a
  path it opens, joins or lists to read; a set of such names, or one checked with
  `in`, is a membership test over text and is not a read,
- joins a repository root to a Markdown file (`ROOT / "README.md"`), or
- calls `inspect.getsource`.

Such a test checks wording, not behaviour, and breaks on every edit of the text
(AGENTS.md, test rules). The one exception per tree is a file named
`test_architecture_guards.py`, the allow-listed home of a guard that has to read
the tree. Markdown a test writes into its own temporary directory is not a
repository document and is not matched: the root must be named like one (`ROOT`,
`REPO`, `REPO_ROOT`, ...) or reached through `Path(__file__).parents`.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_NAME = "test_architecture_guards.py"
SKIPPED_PARTS = {"node_modules", ".venv", "__pycache__", "fixtures"}
ROOT_NAME = re.compile(r"root|repo", re.IGNORECASE)


def unit_test_files(root: Path = ROOT) -> list[Path]:
    return sorted(
        path
        for path in root.glob("**/tests/unit/**/*.py")
        if not SKIPPED_PARTS & set(path.relative_to(root).parts)
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def _is_docs_path(value: str) -> bool:
    return value == "docs" or value.startswith("docs/")


def _names_the_repository(node: ast.AST) -> bool:
    """Whether an expression is a repository root: a ROOT-like name or `__file__` parents."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and ROOT_NAME.search(inner.id):
            return True
        if isinstance(inner, ast.Attribute) and inner.attr in {"parent", "parents"}:
            return True
    return False


def _membership_subjects(tree: ast.AST) -> set[int]:
    """Constants used for membership, not as a path to read.

    `"docs/x.md" in prompt` checks text, and a set display (`ALLOWED = {"docs/x.md"}`)
    is a lookup table.
    """
    subjects = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and all(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
        ):
            subjects.add(id(node.left))
        elif isinstance(node, ast.Set):
            subjects.update(id(element) for element in node.elts)
    return subjects


def violations_in(path: Path, source: str | None = None) -> list[str]:
    if path.name == ALLOWED_NAME:
        return []
    tree = ast.parse(path.read_text() if source is None else source, filename=str(path))
    membership = _membership_subjects(tree)
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if (
            (isinstance(node, ast.Attribute) and node.attr == "getsource")
            or (isinstance(node, ast.Name) and node.id == "getsource")
            or (
                isinstance(node, ast.ImportFrom)
                and node.module == "inspect"
                and any(alias.name == "getsource" for alias in node.names)
            )
        ):
            found.append(f"{line}: inspect.getsource")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _is_docs_path(node.value)
            and id(node) not in membership
        ):
            found.append(f"{line}: reads {node.value!r} from the docs tree")
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
            and node.right.value.endswith(".md")
            and _names_the_repository(node.left)
        ):
            found.append(f"{line}: reads the repository document {node.right.value!r}")
    return [f"{path.relative_to(ROOT)}:{finding}" for finding in sorted(set(found))]


def main() -> int:
    found = [finding for path in unit_test_files() for finding in violations_in(path)]
    if found:
        print(
            "Unit tests must not read docs/, repository Markdown or inspect.getsource "
            f"(only {ALLOWED_NAME} may; see AGENTS.md, test rules):",
            file=sys.stderr,
        )
        for finding in found:
            print(f"  {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
