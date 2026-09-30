"""What central QA can do, declared once.

Every text that tells a model what QA can check — the Architect's criteria
rules, the PO's must-requirement guidance, the QA executor's prompt — and the
pre-QA filter that withholds a criterion QA must never perform are fed from
this catalogue. None of them keeps a list of its own; a test renders each of
them and fails when one does.

An action is how the one QA user acts on the product: which platform it acts
on, the route that performs it and the one line a prompt says about it. The
route is the `qa` CLI's fixed call, a probe the executor writes itself and runs
through `qa probe`, or a platform seed from the probe library that it runs the
same way. What QA never does is declared beside it, each with its reason.

A sandbox probe runs in the executor's container, so its platform is offered
only while the `qa_sandbox` image capability installs that platform's tooling.
`QA_SANDBOX_TOOLING` says which package that is and whether it is installed;
the worker-manager's image-builder test pins it against the install map, and
`qa_actions()` is the one place that drops an action whose platform is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "QA_ACTIONS",
    "QA_NEVER",
    "QA_RUNTIME_CALLS",
    "QA_SANDBOX_TOOLING",
    "QAAction",
    "QACapabilityPlatform",
    "QACapabilityRoute",
    "QANever",
    "QASandboxTooling",
    "http_write_methods",
    "qa_actions",
]


class QACapabilityPlatform(StrEnum):
    """The surface of the product an action reaches."""

    TELEGRAM = "telegram"
    HTTP = "http"
    WEB = "web"
    JOB = "job"


class QACapabilityRoute(StrEnum):
    """How the executor performs an action."""

    #: A fixed call of the `qa` CLI, performed by the QA runtime.
    TOOL = "tool"
    #: A script the executor writes and runs through `qa probe`, in its sandbox.
    SANDBOX_PROBE = "sandbox_probe"
    #: A ready platform seed under the probe library, run through `qa probe`.
    LIBRARY_SEED = "library_seed"


@dataclass(frozen=True)
class QAAction:
    """One thing the QA user can do to the product, and what it observes."""

    platform: QACapabilityPlatform
    action: str
    route: QACapabilityRoute
    #: One line a prompt says about it: what is done, and what is observed.
    wording: str
    #: The `qa` call for a tool, the seed's name for a library seed.
    call: str | None = None
    #: Whether a criterion may be written through it. A diagnostic read that
    #: only the executor uses is not a way to state a requirement.
    criterion: bool = True


@dataclass(frozen=True)
class QANever:
    """One thing QA never does, and why."""

    kind: str
    wording: str
    reason: str
    #: The HTTP methods the entry forbids, for the pre-QA filter.
    http_methods: frozenset[str] = frozenset()


@dataclass(frozen=True)
class QASandboxTooling:
    """The package a sandbox platform's probes need from the `qa_sandbox` image."""

    platform: QACapabilityPlatform
    package: str
    #: True exactly when `CAPABILITY_INSTALL_MAP["QA_SANDBOX"]` installs `package`.
    installed: bool


QA_SANDBOX_TOOLING: tuple[QASandboxTooling, ...] = (
    QASandboxTooling(QACapabilityPlatform.TELEGRAM, "telethon", installed=True),
    # No browser is installed yet, so no web action is offered.
    QASandboxTooling(QACapabilityPlatform.WEB, "playwright", installed=False),
)

_TELEGRAM = QACapabilityPlatform.TELEGRAM
_HTTP = QACapabilityPlatform.HTTP
_JOB = QACapabilityPlatform.JOB
_TOOL = QACapabilityRoute.TOOL
_PROBE = QACapabilityRoute.SANDBOX_PROBE
_SEED = QACapabilityRoute.LIBRARY_SEED

QA_ACTIONS: tuple[QAAction, ...] = (
    QAAction(
        _HTTP,
        "get_route",
        _TOOL,
        "a read-only HTTP GET of a route on the deployed URL, and what it answers",
        call="http_get",
    ),
    QAAction(
        _HTTP,
        "get_route_script",
        _PROBE,
        "a read-only GET of the deployed URL from a script, through the run's proxy",
        criterion=False,
    ),
    QAAction(
        _HTTP,
        "get_loopback",
        _TOOL,
        "a read-only HTTP GET of a port on the deployment host's loopback",
        call="localhost_http_get",
        criterion=False,
    ),
    QAAction(
        _TELEGRAM,
        "send_text",
        _TOOL,
        "a text message or command sent to the bot as the QA user, and the bot's reply",
        call="telegram_probe",
    ),
    QAAction(
        _TELEGRAM,
        "press_button",
        _TOOL,
        "a press of an inline button the bot showed, and what follows, an edit in place included",
        call="telegram_click_button",
    ),
    QAAction(
        _TELEGRAM,
        "send_location",
        _SEED,
        "a location sent to the bot as the QA user, and the bot's reply",
        call="location",
    ),
    QAAction(
        _TELEGRAM,
        "send_contact",
        _PROBE,
        "a contact shared with the bot as the QA user, and the bot's reply",
    ),
    QAAction(
        _TELEGRAM,
        "send_media",
        _PROBE,
        "a photo, file or other media sent to the bot as the QA user, and the bot's reply",
    ),
    QAAction(
        _TELEGRAM,
        "reply",
        _PROBE,
        "a reply to one of the bot's messages sent as the QA user, and the bot's answer",
    ),
    QAAction(
        _TELEGRAM,
        "edit",
        _PROBE,
        "an edit of a message the QA user already sent the bot, and how the bot answers it",
    ),
    QAAction(
        _JOB,
        "fire",
        _TOOL,
        "a declared `FIRE JOB <name> ... THEN <observable>`, judged on its observable",
        call="fire_job",
    ),
    QAAction(
        _JOB,
        "job_evidence",
        _TOOL,
        "a read-back of this run's own record of a job it fired",
        call="job_evidence",
        criterion=False,
    ),
)

QA_NEVER: tuple[QANever, ...] = (
    QANever(
        "http_write",
        "an HTTP POST, PUT, PATCH or DELETE to the product's API",
        "platform policy: QA acts on the product as a user and never writes through its API",
        http_methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
    ),
    QANever(
        "product_state",
        "a direct read or write of the product's stored data or state",
        "QA is a black-box tester: only what the product answers is evidence",
    ),
    QANever(
        "outside_target",
        "anything outside this run's deployment and Telegram",
        "the QA sandbox reaches nothing else",
    ),
)

#: Calls of the `qa` CLI that act on the run or read the deployment host, not
#: on the product. With the tool actions above they are the CLI's whole surface.
QA_RUNTIME_CALLS: frozenset[str] = frozenset(
    {
        "capabilities",
        "remote_read",
        "remote_exec",
        "container_logs",
        "container_inspect",
        "telegram_identity",
        "probe",
        "report",
        "finish",
    }
)


def qa_actions() -> tuple[QAAction, ...]:
    """The actions QA can perform now: a sandbox platform counts only when installed."""
    missing = {tooling.platform for tooling in QA_SANDBOX_TOOLING if not tooling.installed}
    return tuple(
        action
        for action in QA_ACTIONS
        if action.route is QACapabilityRoute.TOOL or action.platform not in missing
    )


def http_write_methods() -> frozenset[str]:
    """Every HTTP method a criterion must never ask QA to send."""
    return frozenset().union(*(never.http_methods for never in QA_NEVER))
