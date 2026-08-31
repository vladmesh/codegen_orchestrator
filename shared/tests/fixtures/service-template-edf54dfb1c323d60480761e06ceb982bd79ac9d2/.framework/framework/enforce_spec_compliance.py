"""Enforce spec-driven development by forbidding manual models and routers."""

import ast
from pathlib import Path
import sys

from framework.lib.env import get_repo_root
from framework.lib.fs import parse_python


def is_violation(
    node: ast.AST,
    *,
    check_base_model: bool = True,
    check_api_router: bool = True,
) -> bool:
    """Check if node represents a violation."""
    # Check for Pydantic BaseModel inheritance
    if check_base_model and isinstance(node, ast.ClassDef):
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseModel":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                return True

    # Check for APIRouter instantiation
    if check_api_router and isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "APIRouter":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "APIRouter":
            return True

    return False


def check_file(
    file_path: Path,
    *,
    check_base_model: bool = True,
    check_api_router: bool = True,
) -> list[tuple[int, str]]:
    """Check a single file for violations."""
    tree = parse_python(file_path)
    if tree is None:
        return []

    violations = []
    lines = file_path.read_text(encoding="utf-8").splitlines()

    for node in ast.walk(tree):
        if is_violation(
            node, check_base_model=check_base_model, check_api_router=check_api_router
        ):
            # Check for noqa on the same line
            lineno = node.lineno
            if lineno <= len(lines):
                line_content = lines[lineno - 1]
                if "# noqa" in line_content or "#noqa" in line_content:
                    continue

            msg = "Violation found"
            if isinstance(node, ast.ClassDef):
                msg = (
                    f"Defining Pydantic model '{node.name}' manually is forbidden. "
                    "Use shared/spec/models.yaml."
                )
            elif isinstance(node, ast.Call):
                msg = "APIRouter should be defined in app/api/routers/, not here."

            violations.append((node.lineno, msg))

    return violations


def main() -> None:
    """Main entry point."""
    repo_root = get_repo_root()
    services_dir = repo_root / "services"

    if not services_dir.exists():
        print("No services directory found.")
        return

    models_file = repo_root / "shared" / "spec" / "models.yaml"
    if not models_file.exists():
        print("No specs found. Skipping spec compliance check.")
        return

    violations_found = False

    for file_path in services_dir.rglob("*.py"):
        # Global skips
        if (
            "migrations" in file_path.parts
            or "tests" in file_path.parts
            or "generated" in file_path.parts
            or ".venv" in file_path.parts
        ):
            continue

        if file_path.name == "__init__.py":
            continue

        in_controllers = "controllers" in file_path.parts
        in_routers = "routers" in file_path.parts
        is_wiring = file_path.name in ("router.py", "health.py")

        # APIRouter check: everywhere except routers/, router.py, health.py
        # BaseModel check: only in controllers/
        file_violations = check_file(
            file_path,
            check_base_model=in_controllers,
            check_api_router=not in_routers and not is_wiring,
        )
        if file_violations:
            violations_found = True
            print(f"\nIn {file_path.relative_to(repo_root)}:")
            for lineno, msg in file_violations:
                print(f"  Line {lineno}: {msg}")

    if violations_found:
        print("\nSpec compliance check FAILED.")
        sys.exit(1)
    else:
        print("Spec compliance check PASSED.")


if __name__ == "__main__":
    main()
