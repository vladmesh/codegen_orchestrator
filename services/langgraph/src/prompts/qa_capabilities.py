"""What QA can check, rendered for each model that needs to know.

The catalogue in `shared.contracts.qa_capabilities` is the only list. Each
audience gets one renderer, and each renderer reads nothing but the catalogue:
the Architect writes criteria through it, the PO words must-requirements
through it, and the QA executor performs it. The pre-QA filter reads its HTTP
write set from the catalogue directly.
"""

from __future__ import annotations

import textwrap

from shared.contracts.dto.product_brief import NOT_AUTOMATICALLY_VERIFIABLE_PREFIX
from shared.contracts.qa_capabilities import (
    QA_NEVER,
    QAAction,
    QACapabilityPlatform,
    QACapabilityRoute,
    qa_actions,
)
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH, QA_PROBE_NAME

__all__ = [
    "render_architect_capabilities",
    "render_brief_capabilities",
    "render_executor_capabilities",
]

_PLATFORM_LABEL = {
    QACapabilityPlatform.TELEGRAM: "Telegram",
    QACapabilityPlatform.HTTP: "HTTP",
    QACapabilityPlatform.WEB: "Web",
    QACapabilityPlatform.JOB: "Scheduled job",
}


def _criterion_actions() -> tuple[QAAction, ...]:
    return tuple(action for action in qa_actions() if action.criterion)


def _never_list() -> str:
    return ";\n".join(f"- {never.wording} — {never.reason}" for never in QA_NEVER) + "."


def render_architect_capabilities() -> str:
    """The Architect's "What QA Can Check" section."""
    actions = ";\n".join(
        f"- {_PLATFORM_LABEL[action.platform]}: {action.wording}" for action in _criterion_actions()
    )
    return f"""\
## What QA Can Check

QA is a black-box tester acting as one QA user, and a criterion is only a check \
when it is stated through what that user can do:

{actions}.

A scheduled job is written in the form "Scheduled Behaviours" gives. QA never \
performs:

{_never_list()}

A behaviour that needs one of these is verified through its observable after \
the fact — a GET that exposes the stored record, or the bot's reply — and never \
as that step. Write "GET /api/transactions lists the recorded transaction", not \
"POST /api/transactions returns 201"; the platform marks a criterion that needs \
an HTTP write as not verifiable and QA never checks it.

{_render_unverifiable_requirement_rule()}
"""


def _render_unverifiable_requirement_rule() -> str:
    """What the Architect does with a must-requirement no action above can check."""
    return f"""\
**A must-requirement QA cannot check is rewritten or returned at planning time.** \
When the only usage example of a must-requirement — or every one of them — needs \
an action this list does not name, or one QA never performs, a criterion written \
as that step is never checked. Two outcomes, in this order:

- **Rewrite the check into an observable QA can check**: the example's effect, \
read afterwards through one of the actions above, in the example's words.
- **Return the requirement** when no such observable exists, with \
`record_requirement_coverage(requirement_id=..., returned_reason=...)`, and start \
the reason with `{NOT_AUTOMATICALLY_VERIFIABLE_PREFIX}` followed by what QA would \
need, e.g. "{NOT_AUTOMATICALLY_VERIFIABLE_PREFIX} QA would need to read the email \
the product sends to the user". The user is told and decides; its examples get \
no criterion."""


def render_brief_capabilities(indent: str) -> str:
    """The PO's guidance on wording a must-requirement, indented for a docstring."""
    actions = "; ".join(action.wording for action in _criterion_actions())
    nevers = "; ".join(never.wording for never in QA_NEVER)
    text = (
        f"Word the text as something QA can check as its one QA user: {actions}. "
        f"QA never performs {nevers}: a behaviour that needs one of these is stated "
        "by its observable after the fact (a GET or a bot reply), never as that step."
    )
    return textwrap.fill(
        text,
        width=88,
        initial_indent=indent,
        subsequent_indent=indent,
        break_on_hyphens=False,
    )


def _route(action: QAAction) -> str:
    platform = action.platform.value
    if action.route is QACapabilityRoute.TOOL:
        return f"`{QA_PROBE_NAME} {action.call}`"
    if action.route is QACapabilityRoute.LIBRARY_SEED:
        return (
            f"the ready library probe `{platform}/{action.call}` listed in "
            f"`{QA_PROBE_LIBRARY_PATH}/index.json`, run through `{QA_PROBE_NAME} probe`"
        )
    return f"a probe you write, run through `{QA_PROBE_NAME} probe {platform} NAME FILE`"


def render_executor_capabilities(*, telegram: bool) -> str:
    """The QA executor's action list and the paragraph on what it cannot do.

    A run with no bot under test is not offered the Telegram actions.
    """
    actions = "\n".join(
        f"- {_PLATFORM_LABEL[action.platform]}: {action.wording} — {_route(action)}"
        for action in qa_actions()
        if telegram or action.platform is not QACapabilityPlatform.TELEGRAM
    )
    telethon = (
        "\nA probe you write for Telegram acts as the QA account through your own Telethon\n"
        f"client (`{QA_PROBE_NAME} telegram_identity`).\n"
        if telegram
        else ""
    )
    return f"""\
## What you can check

Every check is one of these, performed as the platform's QA user:

{actions}
{telethon}
You never perform:

{_never_list()}

A criterion that needs an action this list does not name, or one you never
perform, is a check you cannot make: report it failed with cause
`qa_capability`. It is never a product failure.
"""
