"""Every actor decision receives the credential that authenticated its caller.

`X-Telegram-ID` names an actor only for an internal service.  An LK bearer names
its own immutable user, so a guard that neglects to pass the bearer to
`resolve_actor` silently turns a client-controlled header into that identity.
Keep this structural: a future guard must fail here before it can ship that
omission.
"""

from __future__ import annotations

import ast
from pathlib import Path

API_SRC = Path(__file__).parents[2] / "src"
DECIDER_MODULE = API_SRC / "dependencies.py"
DECIDER = "resolve_actor"

# User registration creates or updates the account that a request names.  It is
# not an actor decision, and cannot use the account as a credential before it
# exists.  Keep every exception named: a third one is a deliberate security
# decision, not an unnoticed way around the caller-principal rule.
TELEGRAM_USER_LOOKUP_EXEMPTIONS = frozenset(
    {
        ("routers/users.py", "create_user"),
        ("routers/users.py", "upsert_user"),
    }
)


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _relative(path: Path) -> str:
    return str(path.relative_to(API_SRC))


def _takes_credentials(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(arg.arg == "credentials" for arg in (*func.args.args, *func.args.kwonlyargs))


def _telegram_header_parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names whose FastAPI default reads the client-controlled Telegram header."""
    positional = [*func.args.posonlyargs, *func.args.args]
    defaults = list(
        zip(
            positional[len(positional) - len(func.args.defaults) :],
            func.args.defaults,
            strict=True,
        )
    )
    defaults += [
        (param, default)
        for param, default in zip(func.args.kwonlyargs, func.args.kw_defaults, strict=True)
        if default is not None
    ]
    return {
        param.arg
        for param, default in defaults
        if default is not None
        and isinstance(default, ast.Call)
        and isinstance(default.func, ast.Name)
        and default.func.id == "Header"
        and any(
            keyword.arg == "alias"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "X-Telegram-ID"
            for keyword in default.keywords
        )
    }


def _is_telegram_user_lookup(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and isinstance(node.left.value, ast.Name)
        and node.left.value.id == "User"
        and node.left.attr == "telegram_id"
    )


def _passes_telegram_header_to_user_resolver(
    func: ast.FunctionDef | ast.AsyncFunctionDef, header_parameters: set[str]
) -> bool:
    """Catch a route that hands its Telegram header to a local user resolver."""
    for call in ast.walk(func):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_resolve_user"
        ):
            continue
        if any(isinstance(arg, ast.Name) and arg.id in header_parameters for arg in call.args):
            return True
        if any(
            isinstance(keyword.value, ast.Name) and keyword.value.id in header_parameters
            for keyword in call.keywords
        ):
            return True
    return False


def test_resolve_actor_requires_the_caller_credential():
    """Forgetting the bearer is a signature error, never a fallback branch."""
    tree = ast.parse(DECIDER_MODULE.read_text())
    decider = next((func for func in _functions(tree) if func.name == DECIDER), None)
    assert decider is not None, f"{DECIDER} is the one actor decider"

    credentials_index = next(
        (index for index, arg in enumerate(decider.args.kwonlyargs) if arg.arg == "credentials"),
        None,
    )
    assert credentials_index is not None, f"{DECIDER} must accept the caller credential"
    assert decider.args.kw_defaults[credentials_index] is None, (
        f"{DECIDER}'s credentials parameter must be required"
    )


def test_every_actor_decision_passes_the_caller_credential():
    """A deliberately reintroduced call without `credentials=` fails here."""
    offenders = []
    for path in sorted(API_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id != DECIDER:
                continue
            if not any(keyword.arg == "credentials" for keyword in call.keywords):
                relative = path.relative_to(API_SRC.parents[2])
                offenders.append(f"{relative}:{call.lineno}")

    assert not offenders, (
        f"every {DECIDER}() call must pass the credential that authenticated the caller:\n"
        + "\n".join(offenders)
    )


def test_telegram_user_resolution_requires_the_caller_credential():
    """A deliberately restored allocation/project guard fails here before it ships."""
    offenders = []
    for path in sorted(API_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        module = _relative(path)
        for func in _functions(tree):
            if (module, func.name) in TELEGRAM_USER_LOOKUP_EXEMPTIONS:
                continue
            header_parameters = _telegram_header_parameters(func)
            if not header_parameters or _takes_credentials(func):
                continue
            direct_lookup = any(_is_telegram_user_lookup(node) for node in ast.walk(func))
            passes_to_resolver = _passes_telegram_header_to_user_resolver(func, header_parameters)
            if direct_lookup or passes_to_resolver:
                offenders.append(f"{_relative(path)}:{func.lineno} in {func.name}()")

    assert not offenders, (
        "a function resolved a user from X-Telegram-ID without receiving the caller "
        "credential; use resolve_actor and keep any registration exception named:\n"
        + "\n".join(offenders)
    )


def test_telegram_user_lookup_exemptions_still_exist():
    """An obsolete exemption must not silently make a later one look reviewed."""
    for module, name in TELEGRAM_USER_LOOKUP_EXEMPTIONS:
        tree = ast.parse((API_SRC / module).read_text())
        assert any(func.name == name for func in _functions(tree)), (
            f"{module} no longer defines {name}: drop it from the exemption list"
        )
