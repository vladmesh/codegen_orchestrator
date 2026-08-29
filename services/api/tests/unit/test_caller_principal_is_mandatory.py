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


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


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
