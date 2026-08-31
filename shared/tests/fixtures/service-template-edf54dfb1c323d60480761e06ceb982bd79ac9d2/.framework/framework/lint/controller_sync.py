"""Controller-Protocol synchronization checker.

Checks that controller classes implement all methods defined in their protocols.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from framework.generators.context import OperationContextBuilder, controller_path
from framework.lib.fs import parse_python
from framework.spec.loader import AllSpecs


@dataclass
class MissingMethod:
    """A method that is missing from a controller."""

    name: str
    params: list[tuple[str, str]]  # [(param_name, param_type), ...]
    return_type: str


@dataclass
class ControllerSyncResult:
    """Result of checking a controller against its protocol."""

    controller_path: Path
    protocol_name: str
    missing_methods: list[MissingMethod]

    @property
    def is_synced(self) -> bool:
        """Check if controller is synchronized with protocol."""
        return len(self.missing_methods) == 0


def get_controller_methods(controller_path: Path) -> set[str]:
    """Extract method names from a controller file using AST."""
    tree = parse_python(controller_path)
    if tree is None:
        return set()

    methods: set[str] = set()

    for node in ast.walk(tree):
        # Look for async function definitions (our handlers are async)
        if isinstance(node, ast.AsyncFunctionDef):
            methods.add(node.name)
        # Also check regular functions for flexibility
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            methods.add(node.name)

    return methods


def check_controller_sync(
    specs: AllSpecs,
    repo_root: Path,
) -> list[ControllerSyncResult]:
    """Check all controllers against their protocols.

    Returns list of results, one per controller.
    """
    results = []
    context_builder = OperationContextBuilder()

    for domain in specs.domains.values():
        path = controller_path(repo_root, domain)
        protocol_name = domain.protocol_name

        # Get actual methods from controller
        actual_methods = get_controller_methods(path)

        # Find missing methods with their signatures
        missing = []
        for operation in domain.operations:
            if operation.name not in actual_methods:
                ctx = context_builder.build_for_protocol(operation)

                params = [(p.name, p.type) for p in ctx.params]
                if ctx.input_model:
                    params.append(("data", ctx.input_model))

                missing.append(
                    MissingMethod(
                        name=operation.name,
                        params=params,
                        return_type=ctx.computed_return_type,
                    )
                )

        results.append(
            ControllerSyncResult(
                controller_path=path,
                protocol_name=protocol_name,
                missing_methods=missing,
            )
        )

    return results


def lint_controllers_cli(repo_root: Path) -> tuple[bool, str]:
    """CLI-friendly controller sync check.

    Returns (success, message).
    """
    from framework.spec.loader import load_specs

    try:
        specs = load_specs(repo_root)
    except Exception as e:  # noqa: BLE001
        return False, f"Failed to load specs: {e}"

    if not specs.models.models:
        return True, "No specs found. Skipping controller sync check."

    results = check_controller_sync(specs, repo_root)

    all_synced = True
    messages = []

    for result in results:
        if not result.is_synced:
            all_synced = False
            missing_names = [m.name for m in result.missing_methods]
            messages.append(
                f"  {result.controller_path.name}: "
                f"Missing {len(missing_names)} method(s): {', '.join(missing_names)}"
            )

    if all_synced:
        return True, "All controllers are synchronized with their protocols."

    return False, (
        "Controller sync check FAILED.\n"
        + "\n".join(messages)
        + "\n\nRun `make generate-from-spec` to add stubs."
    )
