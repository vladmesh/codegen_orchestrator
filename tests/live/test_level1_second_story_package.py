"""Offline regressions for the kit catalog package the second story installs.

sprint:1477 DoD5/DoD6: the mega-noop extension story installs `reminders` with
`kit add`, deploys onto the first story's product, central QA reads
`GET /reminders` as its verified QA identity, and the seeded reminder is emitted
by the kit core's own timer. Each predicate is fed what a green run records and
what the defect it exists for records — no package on the deployment, a 401 at
the route, a reminder still `scheduled` after the wait — and has to say so.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess

from framework.catalog import parse_catalog
from level1_brief import LEVEL1_REMINDERS_OWNER_REF, level1_reminders_owner_ref
from level1_change_set import (
    LEVEL1_EXTENSION_PACKAGE,
    LEVEL1_EXTENSION_PACKAGE_JOB,
    LEVEL1_EXTENSION_PACKAGE_ROUTE,
)
from level1_second_story import (
    package_install_mismatches,
    package_route_qa_mismatches,
    package_route_qa_record,
    reminder_emission_mismatches,
)
from package_route import package_not_active
import pipeline_helpers
import pytest
import run_evidence

from services.langgraph.src.agents.qa import caller_identity
from services.langgraph.src.agents.qa.packages import (
    ACTIVE_PACKAGE_CONTRACT,
    GENERATED_JOB_REGISTRY,
)
from services.langgraph.src.consumers import _qa_redaction
from shared.live_harness_cleanup import PACKAGE_CONTRACT_FILE_MARKER

pytestmark = pytest.mark.needs_no_api_credential

CATALOG_VERSION = "0.4.0"
OWNER = LEVEL1_REMINDERS_OWNER_REF
CHECK = f"GET {LEVEL1_EXTENSION_PACKAGE_ROUTE} returns 200"


def _contract(version: str | None) -> str:
    if version is None:
        return "ACTIVE_PACKAGES = []\n"
    return (
        "ACTIVE_PACKAGES = [\n"
        f'    {{"name": "reminders", "version": "{version}", "manifest_sha256": "{"a" * 64}"}},\n'
        "]\n"
    )


def _probe(version: str | None) -> str:
    registry = 'JOB_SCHEMA_SOURCES = {"reminders.tick": "package:reminders"}\n'
    return (
        f"{PACKAGE_CONTRACT_FILE_MARKER} {ACTIVE_PACKAGE_CONTRACT}\n{_contract(version)}"
        f"{PACKAGE_CONTRACT_FILE_MARKER} {GENERATED_JOB_REGISTRY}\n{registry}"
    )


_CATALOG = """format_version: 1
packages:
  - name: reminders
    distribution: codegen-kit-reminders
    path: packages/codegen-kit-reminders
    summary: One-time text reminders.
    capabilities: [remind me at a time]
    settings:
      - name: reminder_owner_ref
        summary: Owner of the seeded reminder.
    environment: []
    versions:
      - version: 0.3.0
        tag: packages/reminders/v0.3.0
        requires_core: ">=2,<3"
      - version: 0.4.0
        tag: packages/reminders/v0.4.0
        requires_core: ">=2.1,<3"
"""


def _record_install(monkeypatch, *, version: str | None) -> dict:
    """Run the suite's recorder against a deployment and a catalog in these shapes."""
    monkeypatch.setattr(
        pipeline_helpers,
        "read_catalog",
        lambda source, ref: parse_catalog(_CATALOG, f"{source}@{ref}"),
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec_python_module",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, _probe(version), ""),
    )
    ctx = {"project_name": "proj-1", "server_handle": "srv-1"}
    pipeline_helpers.record_level1_package_install(ctx)
    return ctx


def _install_reasons(ctx: dict) -> list[str]:
    return package_install_mismatches(
        ctx.get("level1_package_install"),
        error=ctx.get("level1_package_install_error"),
        package=LEVEL1_EXTENSION_PACKAGE,
        catalog_version=(ctx.get("level1_package_catalog") or {}).get("version"),
        catalog_error=ctx.get("level1_package_catalog_error"),
    )


# ── (a) the deployment records the package at the catalog's version ──────


def test_a_deployment_carrying_the_catalog_version_holds(monkeypatch):
    ctx = _record_install(monkeypatch, version=CATALOG_VERSION)

    assert ctx["level1_package_catalog"]["version"] == CATALOG_VERSION
    assert ctx["level1_package_catalog"]["tag"] == "packages/reminders/v0.4.0"
    assert ctx["level1_package_install"]["package"] == LEVEL1_EXTENSION_PACKAGE
    assert ctx["level1_package_install"]["behaviour"] == LEVEL1_EXTENSION_PACKAGE_JOB
    assert _install_reasons(ctx) == []


def test_a_deployment_that_records_no_package_says_so(monkeypatch):
    ctx = _record_install(monkeypatch, version=None)

    assert "level1_package_install" not in ctx
    assert _install_reasons(ctx) == [
        package_not_active(LEVEL1_EXTENSION_PACKAGE, "no active kit package")
    ]


def test_a_deployment_at_another_version_than_the_catalog_s_says_so(monkeypatch):
    ctx = _record_install(monkeypatch, version="0.3.0")

    assert _install_reasons(ctx) == [
        "the deployment records 'reminders' at version '0.3.0', not the catalog's '0.4.0'"
    ]


def test_an_unread_catalog_is_a_reason_not_a_pass(monkeypatch):
    ctx = _record_install(monkeypatch, version=CATALOG_VERSION)
    ctx.pop("level1_package_catalog")
    ctx["level1_package_catalog_error"] = "the kit catalog could not be read: unreachable"

    assert _install_reasons(ctx) == [
        "the kit catalog's version of 'reminders' was not read: "
        "the kit catalog could not be read: unreachable"
    ]


def test_the_catalog_read_failing_is_recorded_as_a_reason(monkeypatch):
    def unreachable(source, ref):
        raise RuntimeError("catalog source is unreachable")

    ctx = _record_install(monkeypatch, version=CATALOG_VERSION)
    monkeypatch.setattr(pipeline_helpers, "read_catalog", unreachable)
    pipeline_helpers.record_level1_package_install(ctx := {"project_name": "p"})

    assert "level1_package_catalog" not in ctx
    assert "catalog source is unreachable" in ctx["level1_package_catalog_error"]


# ── (b) central QA read the route as its verified QA identity ────────────


def _qa_run(*, status: int = 200, identity: dict | None = None) -> dict:
    """A terminal health-only QA Run as the consumer writes it."""
    passed = status == 200
    detail = f"got {status}" if passed else f"got {status}, expected 200"
    body = "[]" if passed else '{"detail":"Not authenticated"}'
    checks = [("GET /health returns 200", True, "got 200"), (CHECK, passed, detail)]
    return {
        "id": "qa-run-1",
        "status": "completed",
        "run_metadata": {
            "qa_caller_identity": identity
            if identity is not None
            else {"user_ref": OWNER, "active": True}
        },
        "result": {
            "qa_outcome": "passed" if passed else "failed",
            "report": "\n".join(f"- {name}: {text}; body: {body}" for name, _, text in checks),
            "passed_checks": [name for name, ok, _ in checks if ok],
            "failed_checks": [
                {"name": name, "pass": False, "detail": text} for name, ok, text in checks if not ok
            ],
        },
    }


def _qa_reasons(run: dict) -> list[str]:
    return package_route_qa_mismatches(
        package_route_qa_record(run, route=LEVEL1_EXTENSION_PACKAGE_ROUTE), user_ref=OWNER
    )


def test_a_qa_pass_as_the_qa_identity_holds():
    record = package_route_qa_record(_qa_run(), route=LEVEL1_EXTENSION_PACKAGE_ROUTE)

    assert record["report_line"] == f"- {CHECK}: got 200; body: []"
    assert _qa_reasons(_qa_run()) == []


def test_a_401_at_the_route_says_so():
    """The anonymous read the health-only leg made before it carried an identity."""
    reasons = _qa_reasons(_qa_run(status=401))

    assert reasons == [
        "central QA Run qa-run-1 ended 'failed'",
        f"central QA did not pass {CHECK!r}: passed ['GET /health returns 200'], "
        f"failed [{CHECK!r}]",
        f"central QA's retained answer of {CHECK!r} is not a 200: "
        f'\'- {CHECK}: got 401, expected 200; body: {{"detail":"Not authenticated"}}\'',
    ]


def test_a_pass_that_named_no_identity_says_so():
    """A 200 with no recorded identity is not the claim (b) makes."""
    run = _qa_run()
    run["run_metadata"] = {}

    assert _qa_reasons(run) == [
        f"central QA read the deployment as None, not the active identity {OWNER!r}"
    ]


def test_a_qa_that_did_more_than_read_says_so():
    run = _qa_run()
    run["result"]["passed_checks"].append("FIRE JOB reminders.tick")

    assert _qa_reasons(run) == [
        "central QA ran checks that are not GET reads: ['FIRE JOB reminders.tick']"
    ]


def test_an_executor_s_run_is_held_to_its_outcome_and_identity_only():
    """`mega-live`: a real executor names its own checks; the identity still has to match."""
    telegram = level1_reminders_owner_ref(executor_qa=True)
    run = _qa_run(identity={"user_ref": telegram, "active": True})
    run["result"]["passed_checks"] = ["the reminders list shows the seeded reminder"]
    record = package_route_qa_record(run, route=LEVEL1_EXTENSION_PACKAGE_ROUTE)

    assert package_route_qa_mismatches(record, user_ref=telegram, health_only=False) == []
    assert package_route_qa_mismatches(record, user_ref=OWNER, health_only=False) == [
        f"central QA read the deployment as {{'user_ref': {telegram!r}, 'active': True}}, "
        f"not the active identity {OWNER!r}"
    ]


def test_the_seeded_owner_is_the_identity_each_qa_path_reads_as():
    assert level1_reminders_owner_ref(executor_qa=False) == "qa:central-qa"
    assert level1_reminders_owner_ref(executor_qa=True) == "telegram:8202532144"


def test_no_qa_run_at_all_says_so():
    assert package_route_qa_mismatches(None, user_ref=OWNER) == [
        "no central QA Run was recorded for the package route"
    ]


def test_the_suite_keeps_a_redacted_record_of_the_qa_run():
    ctx = {"qa_run": _qa_run()}

    pipeline_helpers.record_level1_package_route_qa(ctx)

    assert ctx["level1_package_route_qa"]["caller_identity"] == {"user_ref": OWNER, "active": True}
    assert ctx["level1_package_route_qa"]["check"] == CHECK


# ── (c) the seeded reminder reached `emitted` with no fire ───────────────


def _reminder(state: str, *, owner: str = OWNER) -> dict:
    return {
        "id": "4b0f0a4e-0000-5000-8000-000000000001",
        "user_ref": owner,
        "state": state,
        "remind_at": "2000-01-01T00:00:00Z",
        "emitted_at": "2026-10-03T07:00:00Z" if state == "emitted" else None,
    }


def _read(elapsed: float, *states: str, status: int = 200) -> dict:
    return {
        "elapsed_seconds": elapsed,
        "user_ref": OWNER,
        "status_code": status,
        "reminders": [_reminder(state) for state in states],
    }


def _emission_reasons(reads: list[dict]) -> list[str]:
    return reminder_emission_mismatches(
        reads,
        owner_ref=OWNER,
        state=pipeline_helpers.LEVEL1_REMINDER_STATE,
        bound_seconds=pipeline_helpers.LEVEL1_REMINDER_EMISSION_BOUND_SECONDS,
    )


def test_an_emitted_reminder_within_the_bound_holds():
    assert _emission_reasons([_read(2.0, "scheduled"), _read(64.5, "emitted")]) == []


def test_a_reminder_still_scheduled_after_the_wait_says_so():
    reads = [_read(2.0, "scheduled"), _read(176.0, "scheduled")]

    assert _emission_reasons(reads) == [
        f"no reminder of {OWNER!r} reached 'emitted' after 176.0 s: ['scheduled']"
    ]


def test_a_read_past_the_bound_says_so_even_when_emitted():
    assert _emission_reasons([_read(240.0, "emitted")]) == [
        "the last read came 240.0 s into the wait, past 180 s"
    ]


def test_a_401_or_an_unread_route_says_so():
    unauthorised = {"elapsed_seconds": 3.0, "status_code": 401, "body": "{}"}
    failed = {"elapsed_seconds": 3.0, "error": "the reminders read exited 1: boom"}

    assert _emission_reasons([unauthorised]) == ["the last read of the reminders answered 401"]
    assert _emission_reasons([failed]) == [
        "the last read of the reminders failed: the reminders read exited 1: boom"
    ]
    assert _emission_reasons([]) == ["the deployment's reminders were never read"]


def test_another_owner_s_reminder_does_not_answer_for_the_seeded_one():
    read = _read(60.0)
    read["reminders"] = [_reminder("emitted", owner="telegram:1")]

    assert _emission_reasons([read]) == [f"no reminder of {OWNER!r} is listed: {read['reminders']}"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_the_wait_stops_at_the_first_emitted_read():
    clock = _Clock()
    answers = iter([("scheduled",), ("scheduled",), ("emitted",)])

    def read(ctx: dict) -> dict:
        states = next(answers)
        return {"status_code": 200, "reminders": [_reminder(state) for state in states]}

    ctx: dict = {"level1_reminders_owner_ref": OWNER}
    reads = await pipeline_helpers.wait_level1_reminder_emitted(
        ctx, read=read, clock=clock, sleep=clock.sleep
    )

    assert [one["elapsed_seconds"] for one in reads] == [0.0, 15.0, 30.0]
    assert ctx["level1_reminder_reads"] is reads
    assert _emission_reasons(reads) == []


@pytest.mark.asyncio
async def test_the_wait_gives_up_inside_the_bound_and_keeps_every_read():
    clock = _Clock()

    def read(ctx: dict) -> dict:
        return {"status_code": 200, "reminders": [_reminder("scheduled")]}

    reads = await pipeline_helpers.wait_level1_reminder_emitted(
        {"level1_reminders_owner_ref": OWNER}, read=read, clock=clock, sleep=clock.sleep
    )

    assert reads[-1]["elapsed_seconds"] <= pipeline_helpers.LEVEL1_REMINDER_EMISSION_BOUND_SECONDS
    assert [one["elapsed_seconds"] for one in reads] == [15.0 * tick for tick in range(13)]
    assert _emission_reasons(reads)[0].startswith(f"no reminder of {OWNER!r} reached 'emitted'")


def _probe_output(payload: dict) -> str:
    return "noise\n" + pipeline_helpers.LEVEL1_REMINDERS_READ_MARKER + json.dumps(payload) + "\n"


def test_a_read_is_parsed_into_states_never_kept_as_the_raw_body():
    body = json.dumps([{**_reminder("emitted"), "text": "Your first reminder is ready."}])

    observation = pipeline_helpers.parse_level1_reminders_read(
        _probe_output({"user_ref": OWNER, "status_code": 200, "body": body})
    )

    assert observation == {
        "user_ref": OWNER,
        "status_code": 200,
        "reminders": [_reminder("emitted")],
    }


def test_a_refused_read_keeps_its_status_and_a_bounded_body():
    observation = pipeline_helpers.parse_level1_reminders_read(
        _probe_output({"user_ref": OWNER, "status_code": 401, "body": "x" * 1000})
    )

    assert observation["status_code"] == 401
    assert len(observation["body"]) == 300
    assert "reminders" not in observation


def test_the_harness_scrubs_its_own_secrets_from_a_read(monkeypatch):
    """The probe already scrubs the run's capabilities; the harness's redaction runs too."""
    monkeypatch.setenv("LIVE_PRODUCT_TOKEN", "tok-0123456789abcdef")
    observation = pipeline_helpers.parse_level1_reminders_read(
        _probe_output({"user_ref": OWNER, "status_code": 500, "body": "tok-0123456789abcdef"})
    )

    assert "tok-0123456789abcdef" not in json.dumps(observation)


def test_the_read_runs_in_langgraph_with_this_deployment_and_no_fire(monkeypatch):
    calls: list[tuple[str, str]] = []

    def exec_in(service: str, script: str, timeout: int = 30):
        calls.append((service, script))
        payload = {"user_ref": OWNER, "status_code": 200, "body": "[]"}
        return subprocess.CompletedProcess([], 0, _probe_output(payload), "")

    monkeypatch.setattr(pipeline_helpers, "docker_exec", exec_in)
    ctx = {
        "project_id": "proj-uuid",
        "deployed_url": "http://203.0.113.5:8123/",
        "level1_reminders_owner_ref": OWNER,
    }

    assert pipeline_helpers.read_level1_reminders(ctx) == {
        "user_ref": OWNER,
        "status_code": 200,
        "reminders": [],
    }
    [(service, script)] = calls
    assert service == "langgraph"
    assert "PROJECT_ID = 'proj-uuid'" in script
    assert "CHANNEL = 'qa'\nEXTERNAL_ID = 'central-qa'" in script
    assert "URL = 'http://203.0.113.5:8123/reminders'" in script
    assert "jobs/fire" not in script
    assert "client.get(URL, headers=identity.headers())" in script
    assert "redaction.text(response.text)" in script
    compile(script, "reminders-read", "exec")


def qa_consumer_path() -> Path:
    return Path(_qa_redaction.__file__).with_name("qa.py")


def test_the_read_imports_only_what_the_langgraph_image_defines():
    """The probe runs against the deployed image's modules; a rename fails here.

    `consumers.qa` cannot be imported outside the langgraph image (it resolves
    `src.*` absolutely), so its one function the probe calls is found by parsing.
    """
    consumer = Path(qa_consumer_path()).read_text(encoding="utf-8")
    stored = [
        node
        for node in ast.parse(consumer).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_stored_secrets"
    ]
    assert [[arg.arg for arg in node.args.args] for node in stored] == [["project_id"]]
    assert callable(_qa_redaction.QARunRedaction.from_stored)
    assert _qa_redaction.USER_IDENTITY_CAPABILITY == "USER_IDENTITY_CAPABILITY"
    identity = caller_identity.QACallerIdentity("qa", "central-qa", "cap")
    assert identity.user_ref == OWNER
    assert set(identity.headers()) == {
        "X-Identity-Capability",
        "X-User-Channel",
        "X-User-External-Id",
    }


def test_a_read_that_did_not_run_is_a_stated_reason(monkeypatch):
    def refuse(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=60)

    monkeypatch.setattr(pipeline_helpers, "docker_exec", refuse)

    assert pipeline_helpers.read_level1_reminders(
        {"project_id": "p", "deployed_url": "http://x", "level1_reminders_owner_ref": OWNER}
    ) == {"error": "the reminders read did not run: TimeoutExpired"}


# ── The evidence document ────────────────────────────────────────────────


def test_the_evidence_document_records_all_three_observations():
    extension = {
        "story_id": "story-2",
        "level1_package_install": {"package": "reminders", "version": CATALOG_VERSION},
        "level1_package_catalog": {"package": "reminders", "version": CATALOG_VERSION},
        "level1_package_route_qa": package_route_qa_record(
            _qa_run(), route=LEVEL1_EXTENSION_PACKAGE_ROUTE
        ),
        "level1_reminder_reads": [_read(64.5, "emitted")],
    }

    section = run_evidence.second_story({"level1_extension": extension})

    captured = run_evidence.CaptureStatus.CAPTURED.value
    for key in ("package_install", "package_catalog", "package_route_qa", "reminder_reads"):
        assert section[key]["status"] == captured, key
    assert section["reminder_reads"]["value"][0]["reminders"][0]["state"] == "emitted"


def test_an_unread_package_install_is_a_stated_absence():
    section = run_evidence.second_story(
        {"level1_extension": {"story_id": "s", "level1_package_install_error": "probe exited 2"}}
    )

    assert section["package_install"]["status"] == run_evidence.CaptureStatus.MISSED.value
    assert section["package_install"]["reason"] == "probe exited 2"
