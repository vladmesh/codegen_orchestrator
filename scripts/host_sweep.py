"""What the host profile of scripts/test-unit-local.sh collects, and what in it is heavy.

`python -m shared` runs the runner with `--host` on the weak control host: every
ALL_SUITES entry except HOST_EXCLUDED_SUITES, with `-m "not ci_only"`. A test there
that starts a denylisted program (docker, ansible, sudo, the kit toolchain, ...)
must carry `ci_only` or one of its sub-markers (scripts/ci_only_markers.py), so the
host deselects it and only the full CI run (`make test-unit`) claims it.

The scan is static. A test file that spawns processes at all is searched for argv
literals naming a denylisted program, and every file for PlaybookCLI. Each hit is
charged to the function holding it; a helper or fixture passes its charge on to the
tests and fixtures that name it, so a marked test may use a heavy helper and an
unmarked one may not. A spawn that runs at import time (a module statement or a
decorator such as `skipif(subprocess.run(...))`) is always a violation: pytest has
imported the module before `-m` can deselect anything.

scripts/tests/test_host_sweep_is_light.py and scripts/check-ci-gate.py both run it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

from scripts.ci_only_markers import CI_ONLY_FAMILY

ROOT = Path(__file__).resolve().parents[1]
TEST_UNIT_LOCAL = ROOT / "scripts" / "test-unit-local.sh"

DENYLIST = frozenset(
    {
        "ansible-playbook",
        "PlaybookCLI",
        "sudo",
        "useradd",
        "runuser",
        "systemctl",
        "docker",
        "xenon",
        "deptry",
        "copier",
    }
)
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
SKIP_DIRS = {".venv", "__pycache__", "node_modules", "fixtures"}

# Calls that start a process. A bare name counts only for the names these modules
# export, imported with `from subprocess import run` and the like.
SPAWN_ATTRIBUTES = {
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "Popen"),
    ("subprocess", "getoutput"),
    ("subprocess", "getstatusoutput"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
    ("os", "system"),
    ("os", "popen"),
    ("os", "execvp"),
    ("os", "execv"),
    ("pty", "spawn"),
}
SPAWN_NAMES = {"check_call", "check_output", "Popen", "create_subprocess_exec"}

# Files the scan flags although the denylisted program never runs for real. Same
# rule as check-ci-gate.py's exclusion lists: a reason per line, on the record.
LIGHT_DESPITE_DENYLIST: dict[str, str] = {}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    owner: str
    program: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.owner} runs {self.program}"


def _suite_table() -> tuple[list[tuple[str, str]], set[str]]:
    script = TEST_UNIT_LOCAL.read_text()
    body = script.partition("ALL_SUITES=(")[2].partition("\n)")[0]
    suites = []
    for line in body.splitlines():
        entry = line.strip()
        if entry.startswith('"'):
            label, _, rest = entry.strip('"').partition("|")
            suites.append((label, rest.partition("|")[0]))
    excluded = re.search(r"^HOST_EXCLUDED_SUITES=\(([^)]*)\)", script, re.MULTILINE)
    if not suites or excluded is None:
        raise RuntimeError("test-unit-local.sh has no ALL_SUITES or HOST_EXCLUDED_SUITES")
    return suites, set(excluded.group(1).split())


def host_suites() -> list[tuple[str, str]]:
    """(label, directory) of every suite the host profile runs."""
    suites, excluded = _suite_table()
    unknown = excluded - {label for label, _ in suites}
    if unknown:
        raise RuntimeError(f"HOST_EXCLUDED_SUITES names unknown suites: {sorted(unknown)}")
    return [(label, directory) for label, directory in suites if label not in excluded]


def host_test_files() -> list[str]:
    """Repo-relative test files the host profile collects."""
    found: set[str] = set()
    for _, directory in host_suites():
        for pattern in TEST_FILE_PATTERNS:
            for path in (ROOT / directory).rglob(pattern):
                relative = path.relative_to(ROOT)
                if not SKIP_DIRS.intersection(relative.parts):
                    found.add(str(relative))
    return sorted(found)


def _program(value: object) -> str | None:
    """The denylisted program a string starts, if it starts one."""
    if not isinstance(value, str):
        return None
    if "PlaybookCLI" in value:
        return "PlaybookCLI"
    words = value.split()
    if not words:
        return None
    name = PurePosixPath(words[0]).name
    return name if name in DENYLIST else None


def _dotted(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None


def _is_spawn(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in SPAWN_NAMES
    return _dotted(call.func) in SPAWN_ATTRIBUTES


def _marker_names(node: ast.AST) -> set[str]:
    """`pytest.mark.<name>` and `pytest.mark.<name>(...)` found in an expression."""
    names = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "mark"
        ):
            names.add(child.attr)
    return names


def _pytestmark(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            names |= _marker_names(statement.value)
    return names


@dataclass
class _Owner:
    """A function, method or module-level name a hit is charged to."""

    name: str
    is_test: bool
    markers: set[str]
    references: set[str]
    hits: list[tuple[int, str]]


def _references(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
    return names


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _heavy_expression(node: ast.AST, programs: dict[str, str]) -> str | None:
    """The denylisted program an argv expression starts, if it starts one.

    A string (a shell line or argv[0]), a list or tuple whose first element starts
    one, `*name` or `name` bound to such a value, `a + b`, or `shutil.which(...)`
    of a denylisted name.
    """
    if isinstance(node, ast.Constant):
        return _program(node.value)
    if isinstance(node, ast.List | ast.Tuple):
        return _heavy_expression(node.elts[0], programs) if node.elts else None
    if isinstance(node, ast.Starred):
        return _heavy_expression(node.value, programs)
    if isinstance(node, ast.Name):
        return programs.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _heavy_expression(node.left, programs) or _heavy_expression(node.right, programs)
    if isinstance(node, ast.Call) and _dotted(node.func) == ("shutil", "which") and node.args:
        return _heavy_expression(node.args[0], programs)
    return None


def _assignments(node: ast.AST) -> list[tuple[str, ast.AST]]:
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            targets, value = child.targets, child.value
        elif isinstance(child, ast.AnnAssign | ast.AugAssign) and child.value is not None:
            targets, value = [child.target], child.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found.append((target.id, value))
    return found


def _programs(node: ast.AST, inherited: dict[str, str]) -> dict[str, str]:
    """Names bound, inside node, to a value that starts a denylisted program."""
    programs = dict(inherited)
    assignments = _assignments(node)
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            program = _heavy_expression(value, programs)
            if program and name not in programs:
                programs[name] = program
                changed = True
    return programs


def _wrappers(functions: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> set[str]:
    """Local functions that hand one of their parameters to a process spawn."""
    # What flows from each function's parameters, and the calls it makes.
    flows: list[tuple[str, set[str], list[ast.Call]]] = []
    for function in functions:
        derived = {arg.arg for arg in ast.walk(function.args) if isinstance(arg, ast.arg)}
        derived.discard("self")
        assignments = [(name, _references(value)) for name, value in _assignments(function)]
        grown = True
        while grown:
            grown = False
            for name, references in assignments:
                if name not in derived and references & derived:
                    derived.add(name)
                    grown = True
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        flows.append((function.name, derived, calls))

    wrappers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, derived, calls in flows:
            if name in wrappers:
                continue
            for call in calls:
                if _is_spawn(call):
                    arguments = call.args[:1]
                elif _callee(call) in wrappers:
                    arguments = call.args
                else:
                    continue
                if any(_references(argument) & derived for argument in arguments):
                    wrappers.add(name)
                    changed = True
                    break
    return wrappers


def _hits(node: ast.AST, programs: dict[str, str], wrappers: set[str]) -> list[tuple[int, str]]:
    """(line, program) of every heavy process start or PlaybookCLI use inside node."""
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name | ast.Attribute):
            name = child.id if isinstance(child, ast.Name) else child.attr
            if name == "PlaybookCLI":
                found.append((child.lineno, "PlaybookCLI"))
        elif isinstance(child, ast.ImportFrom) and any(
            alias.name == "PlaybookCLI" for alias in child.names
        ):
            found.append((child.lineno, "PlaybookCLI"))
        elif isinstance(child, ast.Constant) and _program(child.value) == "PlaybookCLI":
            found.append((child.lineno, "PlaybookCLI"))
        elif isinstance(child, ast.Call):
            if _is_spawn(child):
                arguments = child.args[:1]
            elif _callee(child) in wrappers:
                arguments = child.args
            else:
                continue
            for argument in arguments:
                program = _heavy_expression(argument, programs)
                if program:
                    found.append((child.lineno, program))
    return found


def _import_time_nodes(tree: ast.Module) -> list[ast.AST]:
    """What runs when pytest imports the module: statements, class bodies, decorators."""
    pending: list[ast.AST] = []
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            pending.extend(statement.decorator_list)
        elif isinstance(statement, ast.ClassDef):
            pending.extend(statement.decorator_list)
            for member in statement.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    pending.extend(member.decorator_list)
                else:
                    pending.append(member)
        else:
            pending.append(statement)
    return pending


def violations_in(path: Path) -> list[Violation]:
    """Heavy calls in one test file that no ci_only-family marker covers."""
    relative = str(path.relative_to(ROOT))
    if relative in LIGHT_DESPITE_DENYLIST:
        return []
    source = path.read_text()
    # A file that never names a denylisted program cannot start one.
    if not any(program in source for program in DENYLIST):
        return []
    tree = ast.parse(source, filename=relative)
    module_markers = _pytestmark(tree.body)

    functions: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]] = []
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.append((statement, None))
        elif isinstance(statement, ast.ClassDef):
            for member in statement.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    functions.append((member, statement))
    wrappers = _wrappers([function for function, _ in functions])
    import_time = ast.Module(body=_import_time_nodes(tree), type_ignores=[])
    module_programs = _programs(import_time, {})

    owners: list[_Owner] = []
    for function, cls in functions:
        markers = set(module_markers)
        for decorator in function.decorator_list:
            markers |= _marker_names(decorator)
        if cls is not None:
            for decorator in cls.decorator_list:
                markers |= _marker_names(decorator)
            markers |= _pytestmark(cls.body)
        is_test = function.name.startswith("test") and (cls is None or cls.name.startswith("Test"))
        body = ast.Module(body=function.body, type_ignores=[])
        owners.append(
            _Owner(
                name=function.name,
                is_test=is_test,
                markers=markers,
                references=_references(function.args) | _references(body),
                hits=_hits(body, _programs(body, module_programs), wrappers),
            )
        )

    heavy = {owner.name: owner.hits[0] for owner in owners if owner.hits}
    changed = True
    while changed:
        changed = False
        for owner in owners:
            if owner.name in heavy:
                continue
            reached = sorted(owner.references & heavy.keys())
            if reached:
                heavy[owner.name] = heavy[reached[0]]
                changed = True

    # A spawn at import time runs before `-m` deselects anything, marker or not.
    found = [
        Violation(relative, line, "import time", program)
        for line, program in _hits(import_time, module_programs, wrappers)
        if program != "PlaybookCLI"
    ]
    for owner in owners:
        if owner.is_test and owner.name in heavy and not owner.markers & CI_ONLY_FAMILY:
            line, program = heavy[owner.name]
            found.append(Violation(relative, line, owner.name, program))
    return found


def host_violations() -> list[Violation]:
    """Every heavy test the host profile would run."""
    found: list[Violation] = []
    for relative in host_test_files():
        found.extend(violations_in(ROOT / relative))
    return found


if __name__ == "__main__":
    for violation in host_violations():
        print(violation)
