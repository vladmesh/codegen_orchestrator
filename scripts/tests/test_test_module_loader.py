"""Path-loading regressions and a guard over the test tree's import boundary."""

import ast
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest

from shared.tests import source_index
from tests.path_loader import load_module


def _unregistered_executions(node, registered=frozenset()):
    """Follow statement order, keeping conditional registrations inside their block."""
    violations = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        registered = frozenset()
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exec_module"
        and (not node.args or ast.unparse(node.args[0]) not in registered)
    ):
        violations.append(node.lineno)
    for _field, value in ast.iter_fields(node):
        children = value if isinstance(value, list) else [value]
        block_registered = set(registered)
        for child in children:
            if not isinstance(child, ast.AST):
                continue
            violations.extend(_unregistered_executions(child, block_registered))
            if isinstance(child, ast.Assign) and isinstance(child.value, ast.Name):
                for target in child.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and ast.unparse(target.value) == "sys.modules"
                    ):
                        block_registered.add(child.value.id)
    return violations


@pytest.mark.slow(reason="parses and scans the entire test tree for path-import boundaries")
def test_path_loaders_register_before_execution():
    """Direct loaders must publish the exact module before executing its body."""
    violations = [
        f"{path.relative_to(source_index.ROOT)}:{line}"
        for path in source_index.python_files(source_index.ROOT / "tests")
        for line in _unregistered_executions(source_index.tree(path))
    ]
    assert not violations, f"module execution without prior sys.modules registration: {violations}"


@pytest.mark.parametrize(
    ("source", "unregistered"),
    [
        ("loader.exec_module(module)", True),
        ("try:\n loader.exec_module(module)\nexcept Exception:\n pass", True),
        ("loader.exec_module(module)\nsys.modules[name] = module", True),
        ("if flag:\n sys.modules[name] = module\nloader.exec_module(module)", True),
        ("sys.modules[name] = other\nloader.exec_module(module)", True),
        ("sys.modules[name] = module\nloader.exec_module(module)", False),
        ("sys.modules[name] = module\ntry:\n loader.exec_module(module)\nfinally:\n pass", False),
    ],
)
def test_registration_guard_handles_order_and_nested_blocks(source, unregistered):
    assert bool(_unregistered_executions(ast.parse(source))) is unregistered


@pytest.fixture
def module_name(monkeypatch):
    name = "_test_registered_path_loader"
    monkeypatch.delitem(sys.modules, name, raising=False)
    yield name
    sys.modules.pop(name, None)


@pytest.fixture
def dataclass_source(tmp_path):
    path = tmp_path / "dataclass_fixture.py"
    path.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from typing import ClassVar\n"
        "import sys\n"
        "assert sys.modules[__name__].__dict__ is globals()\n"
        "@dataclass\n"
        "class OwnershipManifest:\n"
        "    run_id: str\n"
        "    marker: ClassVar[str] = 'registered'\n"
    )
    return path


def test_deferred_dataclass_and_fresh_repeat(dataclass_source, module_name):
    first = load_module(module_name, dataclass_source)
    assert first.OwnershipManifest("first").run_id == "first"
    assert list(first.OwnershipManifest.__dataclass_fields__) == ["run_id", "marker"]
    first.OwnershipManifest.marker = "patched"
    second = load_module(module_name, dataclass_source)
    assert second is not first
    assert second.OwnershipManifest.marker == "registered"
    assert second.OwnershipManifest("second").__dict__ == {"run_id": "second"}
    assert sys.modules[module_name] is second


@pytest.mark.parametrize("previous", ["absent", "module", "none"])
@pytest.mark.parametrize("failure", ["RuntimeError", "KeyboardInterrupt", "SyntaxError"])
def test_failed_execution_restores_registration(monkeypatch, module_name, previous, failure):
    old = ModuleType(module_name) if previous == "module" else None
    if previous != "absent":
        monkeypatch.setitem(sys.modules, module_name, old)
    # Supply source at the filesystem boundary; execution and its exceptions use
    # the real SourceFileLoader, without disk writes in each parametrized unit.
    source = "invalid syntax!" if failure == "SyntaxError" else f"raise {failure}('broken')"
    monkeypatch.setattr(
        importlib.machinery.SourceFileLoader,
        "get_code",
        lambda _loader, _name: compile(source, "failure.py", "exec"),
    )
    path = "failure.py"
    with pytest.raises((RuntimeError, KeyboardInterrupt, SyntaxError)):
        load_module(module_name, path)
    if previous == "absent":
        assert module_name not in sys.modules
    else:
        assert sys.modules[module_name] is old
    source = "value = 'recovered'\n"
    assert load_module(module_name, path).value == "recovered"


@pytest.mark.parametrize("spec", [None, importlib.util.spec_from_loader("no_loader", loader=None)])
def test_missing_loader_preserves_existing_module(monkeypatch, module_name, spec):
    old = ModuleType(module_name)
    monkeypatch.setitem(sys.modules, module_name, old)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: spec)
    with pytest.raises(ImportError, match="Cannot load module"):
        load_module(module_name, "missing.py")
    assert sys.modules[module_name] is old


_LOADER_CASES = [
    ("tests/integration/backend/test_run_ownership_propagation.py", "_ownership_manifest"),
    ("tests/integration/backend/test_notification_recipient.py", "_load_bot_delivery"),
    ("tests/live/test_cleanup_ssh_user.py", "_load_clean_live_tests"),
    ("tests/live/test_cleanup_auth_fk.py", "_load_clean_live_tests"),
    ("tests/live/test_cleanup_managed_targets.py", "_load_clean_live_tests"),
    ("tests/live/test_db_teardown.py", "_sweep_module"),
]


@pytest.mark.parametrize(("relative", "function_name"), _LOADER_CASES)
def test_existing_loader_with_deferred_dataclass(
    monkeypatch, dataclass_source, relative, function_name
):
    """Run each real loader against a harmless fixture, without its service stack.

    The source index isolates only the loader function; no live test, conftest,
    API client or Docker fixture is imported or executed.
    """
    path = source_index.ROOT / relative
    function = next(
        node
        for node in source_index.tree(path).body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    namespace = {
        "__file__": str(path),
        "Path": Path,
        "os": os,
        "sys": sys,
        "importlib": importlib,
        "load_module": load_module,
    }
    spec_from_file_location = importlib.util.spec_from_file_location

    def fixture_spec(name, _path):
        monkeypatch.setitem(sys.modules, name, None)
        return spec_from_file_location(name, dataclass_source)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", fixture_spec)
    exec(  # noqa: S102 — execute only a repository-owned loader against the harmless fixture
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace
    )
    args = ["run-regression"] if function_name == "_ownership_manifest" else []
    result = namespace[function_name](*args)
    if args:
        assert result.run_id == "run-regression"
        module = sys.modules[type(result).__module__]
    else:
        module = result
    assert sys.modules[module.__name__] is module
    assert module.OwnershipManifest.marker == "registered"
