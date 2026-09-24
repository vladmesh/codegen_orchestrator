#!/usr/bin/env python3
"""Which docker jobs and matrix legs a CI run needs, from the paths it changed.

detect-changes runs dorny/paths-filter and hands its ``changes`` output (the names of
the filters that matched) to this script, which writes three job outputs:

* ``service-legs`` and ``integration-legs``: JSON lists of the test-service and
  test-integration legs to run. ci.yml builds both matrices from them, so a leg with
  nothing to test is never created and takes no runner, and a job whose list is empty
  is skipped as a whole;
* ``service-image-imports``: ``true`` when the service image import check must run.

merge-gate accepts a skipped test-service, test-integration or service-image-imports
only when these outputs say the run planned nothing for it (``[]`` or ``false``), so a
job skipped for any other reason still fails the gate.

The tables below are the one statement of which change triggers which suite;
scripts/check-ci-gate.py reads them, and scripts/tests/test_ci_plan.py pins them.
Standard library only: detect-changes runs it on the runner's own python3.
"""

from __future__ import annotations

import json
import os
import sys

# A change to any of these reaches every suite: the code every service imports, the
# test harness, the CI definition and the dependency set.
COMMON_TRIGGERS = ("shared", "packages", "docker-test", "ci", "deps", "integration-tests")

# The template suite renders the pinned product kit and runs copier; it imports nothing
# from shared/ or packages/, so only the harness, CI, deps and the scaffolder reach it.
TEMPLATE_TRIGGERS = ("docker-test", "integration-tests", "ci", "deps", "scaffolder")

# leg -> the filters of the services its compose file builds and exercises.
SERVICE_LEGS: dict[str, tuple[str, ...]] = {
    "api": ("api", *COMMON_TRIGGERS),
    "langgraph": ("langgraph", *COMMON_TRIGGERS),
    "scheduler": ("scheduler", *COMMON_TRIGGERS),
    "telegram_bot": ("telegram", *COMMON_TRIGGERS),
    # tests/compose/service/worker-manager.yml builds and starts the worker broker too.
    "worker-manager": ("worker-manager", "worker-broker", *COMMON_TRIGGERS),
    "infra": ("infra-service", *COMMON_TRIGGERS),
}
INTEGRATION_LEGS: dict[str, tuple[str, ...]] = {
    "backend": ("api", "langgraph", *COMMON_TRIGGERS),
    "template": TEMPLATE_TRIGGERS,
    "frontend": ("telegram", "api", *COMMON_TRIGGERS),
    "infra": ("scheduler", "infra-service", "api", *COMMON_TRIGGERS),
    "po-tools": ("langgraph", "api", *COMMON_TRIGGERS),
}
# The service-images filter lists every Python service image's sources, shared/,
# packages/, the compose files and the check's own scripts.
SERVICE_IMAGE_IMPORT_TRIGGERS = ("service-images", "deps", "ci")

# A manual dispatch is asked for exactly to run every leg.
RUN_ALL_EVENT = "workflow_dispatch"
# The service image import check runs on every push (only main pushes run CI) whatever
# it changed, so every commit the release chain publishes had its entrypoints imported.
ALWAYS_IMPORT_EVENTS = ("push", "workflow_dispatch")


def planned_legs(legs: dict[str, tuple[str, ...]], changes: set[str], event: str) -> list[str]:
    if event == RUN_ALL_EVENT:
        return list(legs)
    return [leg for leg, triggers in legs.items() if changes.intersection(triggers)]


def plan(changes: set[str], event: str) -> dict[str, str]:
    imports = event in ALWAYS_IMPORT_EVENTS or bool(
        changes.intersection(SERVICE_IMAGE_IMPORT_TRIGGERS)
    )
    return {
        "service-legs": json.dumps(planned_legs(SERVICE_LEGS, changes, event)),
        "integration-legs": json.dumps(planned_legs(INTEGRATION_LEGS, changes, event)),
        "service-image-imports": "true" if imports else "false",
    }


def read_changes(raw: str) -> set[str]:
    try:
        changes = json.loads(raw)
    except json.JSONDecodeError:
        changes = None
    if not isinstance(changes, list) or not all(isinstance(name, str) for name in changes):
        raise SystemExit(f"CHANGES is not a JSON list of filter names: {raw!r}")
    return set(changes)


def main() -> int:
    event = os.environ.get("EVENT_NAME", "")
    if not event:
        raise SystemExit("EVENT_NAME is required")
    changes = read_changes(os.environ.get("CHANGES", ""))
    outputs = plan(changes, event)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as github_output:
        for name, value in outputs.items():
            github_output.write(f"{name}={value}\n")
            print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
