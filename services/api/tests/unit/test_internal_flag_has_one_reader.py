"""Who is acting on a request is decided in one place, and only there.

The rule is one sentence: a valid `X-Internal-Key` authenticates a service, but a
request that also names a user is judged as that user. It was written out by hand
in each guard, so it could be — and was — applied in `projects.py` and missed in
`runs.py`, which is how a Telegram user could read a stranger's run.

`resolve_actor` is the one reader of the internal flag now. Everyone else may pass
the flag along, never act on it: `if is_internal: return` anywhere else is the
regression this test exists to catch, including in a guard nobody has written yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_SRC = Path(__file__).parents[2] / "src"
DECIDER = "resolve_actor"
DECIDER_MODULE = API_SRC / "dependencies.py"

# What the flag is called where it is taken: the dependency that produces it, and
# the parameter names the routers and guards give it.
FLAG_SOURCE = "is_internal_service"
FLAG_NAMES = frozenset({"is_internal", "_is_internal"})

# `resolve_actor` answers "who is acting". Two questions are not that one, cannot
# be asked of it, and are answered from the key alone. Both are listed here by
# name so that adding a third is a decision somebody makes on purpose.
#
# - `require_authenticated_caller` asks "is this caller anybody at all". It runs
#   before any router, and it must not resolve a user: a bare `X-Telegram-ID` is
#   exactly the forged identity the gate exists to refuse, and `resolve_actor`
#   would hand it back as an actor.
# - `_reject_admin_flag_from_outside` asks "may this caller decide `is_admin`",
#   and only a service may. `resolve_actor` cannot answer it either: the bot
#   registers a user while naming that same user in `X-Telegram-ID`, so resolving
#   the actor would 404 on the very account being created.
ANSWERED_FROM_THE_KEY_ALONE = frozenset(
    {
        ("dependencies.py", "require_authenticated_caller"),
        ("routers/users.py", "_reject_admin_flag_from_outside"),
    }
)


def _sources() -> list[Path]:
    return sorted(p for p in API_SRC.rglob("*.py") if p.is_file())


def _relative(path: Path) -> str:
    return str(path.relative_to(API_SRC.parents[2]))


def _flag_parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters that carry the internal flag, by name or by their dependency."""
    args = func.args
    taken = (*args.posonlyargs, *args.args, *args.kwonlyargs)
    names = {a.arg for a in taken if a.arg in FLAG_NAMES}

    positional = [*args.posonlyargs, *args.args]
    with_defaults = list(
        zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
    )
    with_defaults += [
        (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
    ]
    for arg, default in with_defaults:
        if FLAG_SOURCE in ast.dump(default):
            names.add(arg.arg)
    return names


def _reads_outside_a_call(
    func: ast.FunctionDef | ast.AsyncFunctionDef, flags: set[str]
) -> list[int]:
    """Lines where the flag is used for anything but being handed to a callee.

    Passing it on — to `resolve_actor`, or to a guard that asks `resolve_actor` —
    is how a router forwards what it was given. Reading it is deciding, and that
    decision has one home.
    """
    passed_on: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for value in (*node.args, *(kw.value for kw in node.keywords)):
            if isinstance(value, ast.Name) and value.id in flags:
                passed_on.add(id(value))

    return sorted(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and node.id in flags and id(node) not in passed_on
    )


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def test_only_the_decider_reads_the_internal_flag():
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text())
        module = str(path.relative_to(API_SRC))
        for func in _functions(tree):
            if path == DECIDER_MODULE and func.name == DECIDER:
                continue
            if (module, func.name) in ANSWERED_FROM_THE_KEY_ALONE:
                continue
            flags = _flag_parameters(func)
            if not flags:
                continue
            for line in _reads_outside_a_call(func, flags):
                offenders.append(f"{_relative(path)}:{line} in {func.name}()")

    assert not offenders, (
        f"the internal flag is read outside {DECIDER}(); a guard that acts on it "
        "decides for itself who the caller is, and that is how the rule ends up "
        f"applied in one router and missed in another:\n" + "\n".join(offenders)
    )


def test_the_decider_exists_and_answers_with_the_acting_user():
    tree = ast.parse(DECIDER_MODULE.read_text())
    decider = next((f for f in _functions(tree) if f.name == DECIDER), None)
    assert decider is not None, f"{DECIDER} is the one place the rule lives"

    params = {a.arg for a in (*decider.args.args, *decider.args.kwonlyargs)}
    assert {"is_internal", "telegram_id"} <= params, (
        f"{DECIDER} decides from the key and the named user together, got {sorted(params)}"
    )


def test_the_exempt_readers_still_exist():
    """An exemption that outlives its function would silently open a hole."""
    for module, name in ANSWERED_FROM_THE_KEY_ALONE:
        tree = ast.parse((API_SRC / module).read_text())
        assert any(f.name == name for f in _functions(tree)), (
            f"{module} no longer defines {name}: drop it from the exemption list"
        )


@pytest.mark.parametrize(
    ("module", "guard"),
    [
        ("routers/projects_guards.py", "check_project_access"),
        ("routers/runs.py", "_check_run_access"),
        ("dependencies.py", "require_internal_or_admin"),
    ],
)
def test_each_access_guard_asks_the_decider(module: str, guard: str):
    """The guards that exist today, pinned: each one asks rather than decides."""
    tree = ast.parse((API_SRC / module).read_text())
    func = next((f for f in _functions(tree) if f.name == guard), None)
    assert func is not None, f"{module} no longer defines {guard}"

    called = {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert DECIDER in called, f"{guard} must take its answer from {DECIDER}"
