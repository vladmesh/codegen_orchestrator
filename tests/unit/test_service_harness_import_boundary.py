"""Production service code must not depend on live-harness implementation modules."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
FORBIDDEN_PREFIX = "shared.live_harness"


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name for alias in node.names if alias.name.startswith(FORBIDDEN_PREFIX)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith(FORBIDDEN_PREFIX):
                imports.append(module)
            elif module == "shared":
                imports.extend(
                    f"shared.{alias.name}"
                    for alias in node.names
                    if alias.name.startswith("live_harness")
                )
    return imports


def test_production_services_do_not_import_live_harness_modules() -> None:
    offenders = {
        str(path.relative_to(ROOT)): imports
        for path in SERVICES.glob("*/src/**/*.py")
        if (imports := _forbidden_imports(path))
    }

    assert offenders == {}
