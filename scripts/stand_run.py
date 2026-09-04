#!/usr/bin/env python3
"""Run an e2e suite on the stand: one entry point, one place that knows how.

Everything here was a hand-written shell script once, and each of them learned
the same lessons separately and then died with the session that wrote it. The
lessons are the reason this file exists:

* **The QA executor is decided by the API, not by the consumer that obeys it.**
  `resolve_executor_decision` runs inside `api` and persists its answer on the
  paid Run; `qa-worker` only carries it out. Switching the executor therefore
  means rewriting `.env`, recreating *every* service compose gives the variable
  to, and then asking the resolver — not a consumer — what it now answers. Run
  33743251165 asked for `claude`, recreated `qa-worker` alone, and ran QA on the
  Codex the untouched `api` container was still resolving to.

* **A switch is a property of running containers.** After the recreate the old
  containers keep answering for a few seconds and will happily report the
  executor you just replaced, so the confirmation *waits*.

* **An exported variable outranks `.env`.** Compose interpolates
  `${QA_EXECUTOR_AGENT_TYPE:-codex}` from the process environment first, so a
  runner that sourced `.env` into its own environment pins the executor it is
  trying to change. Every compose call here is made with that name removed.

* **A run outlives the SSH session that starts it.** A mega takes ten minutes,
  a matrix an hour; both are longer than a connection reliably lives. Start this
  detached (`setsid nohup`) and read the log it names.

Suites are a table, not code paths, so a new one is a line:

    ./scripts/stand_run.py --suite mega-noop
    ./scripts/stand_run.py --suite mega-llm --worker codex --qa claude
    ./scripts/stand_run.py --suite mega-brief --worker codex --qa claude
    ./scripts/stand_run.py --suite matrix
    ./scripts/stand_run.py --suite tests/live/test_api_crud.py
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread
import time
import urllib.request
from xml.etree import ElementTree

import yaml

from shared.contracts.worker_evidence import secret_env_values
from shared.diagnostics import redact_diagnostic
from shared.stand_deadlines import MEGA_BRIEF_HARD_STOP_SECONDS, MEGA_BRIEF_PRODUCTIVE_SECONDS

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.stand.yml")
QA_EXECUTOR_ENV = "QA_EXECUTOR_AGENT_TYPE"
AGENTS = ("claude", "codex")
RUN_ROOT = Path.home() / "e2e-runs"
EXECUTOR_SWITCH_TIMEOUT_SECONDS = 180
# Recreating a container and waiting for it are one operation here, and this is
# that wait's budget. Deliberately the same policy as the workflow's own wait
# (.github/workflows/stand-e2e.yml, "Bring up dynamic orchestrator and wait for
# API"): probe, and on failure sleep five seconds, until 180 seconds are spent.
# The two cannot literally share code — that one is bash on the host, this one is
# Python in the runner — so the behaviour is copied and the policy stays single.
READINESS_TIMEOUT_SECONDS = 180
READINESS_POLL_SECONDS = 5
# The base URL `tests/live/conftest.py` builds every client on. Readiness is
# established from here, over the network the suite uses, and not with
# `compose exec … curl 127.0.0.1` the way the workflow can afford to: run
# 33749154999 died of an `httpx.ReadError` raised in the pytest process while
# the container itself was already answering itself. An in-container probe would
# have passed and the suite would still have failed.
SUITE_API_BASE_URL = "http://localhost:8000"
API_HEALTH_PATH = "/health"
API_HEALTH_OK_STATUS = 200
# Every compose verb that can leave a container running. Each one has to go
# through the gate, so that "recreate" and "wait" cannot be separated by a
# future caller who only needs the first half.
COMPOSE_LIFECYCLE_COMMANDS = ("up", "start", "restart")
#: Set only inside `recreate_and_wait`; `_compose` refuses a lifecycle verb
#: outside it. This is what makes the gate the only door rather than the
#: politest one.
_INSIDE_RECREATE_GATE = False
# The noop lifecycle has 3,680s of explicit waits at its worst case: scaffold,
# two ordered engineering Tasks, story aggregation, deploy/run/outcome, the
# bounded public health probe, QA, completed-story/PO delivery, deployment
# record, undeploy Run and terminal resource release. The cap leaves 820s for
# manifest teardown and diagnostics; the LLM route does not run this lifecycle
# acceptance yet. See tests/live/README.md for the ledger.
NOOP_SUITE_TIMEOUT_SECONDS = 4500
LLM_SUITE_TIMEOUT_SECONDS = 3600
CUSTOM_TARGET_TIMEOUT_SECONDS = 2700
PREFLIGHT_TIMEOUT_SECONDS = 300
SWEEP_TIMEOUT_SECONDS = 300

# A productive mega-brief receives fifty minutes from its fixture-owned clock.
# The runner is deliberately a later backstop: the remaining window belongs to
# the fixture's evidence and cleanup finalizers before any process-group kill.
BRIEF_SUITE_TIMEOUT_SECONDS = MEGA_BRIEF_PRODUCTIVE_SECONDS
BRIEF_HARD_STOP_SECONDS = MEGA_BRIEF_HARD_STOP_SECONDS
BRIEF_CLEANUP_GRACE_SECONDS = BRIEF_HARD_STOP_SECONDS - BRIEF_SUITE_TIMEOUT_SECONDS
PROCESS_GROUP_TERMINATION_GRACE_SECONDS = 5
RELAY_JOIN_TIMEOUT_SECONDS = 5
BRIEF_RUNNER_TIMEOUT_SECONDS = (
    PREFLIGHT_TIMEOUT_SECONDS
    + READINESS_TIMEOUT_SECONDS
    + EXECUTOR_SWITCH_TIMEOUT_SECONDS
    + BRIEF_HARD_STOP_SECONDS
    + SWEEP_TIMEOUT_SECONDS
)

# The workflow provisions a disposable pair before invoking this runner. Its
# bounded operations total 2,280s (two machine waits, DNS, API readiness, and
# target provisioning). The old target-oriented control-plane bootstrap measured
# about seven minutes; the minimal replacement is expected to need 2–3 minutes,
# pending a live confirmation. The overall provisioning bound remains unchanged.
STAND_PROVISIONING_TIMEOUT_SECONDS = 2700
# A matrix has four 60-minute LLM cells.  Each cell can require a full executor
# switch; runner preflight and the fail-closed sweep have their own bounds.
MATRIX_RUNNER_TIMEOUT_SECONDS = (
    PREFLIGHT_TIMEOUT_SECONDS
    + len(("claude", "codex")) ** 2
    * (LLM_SUITE_TIMEOUT_SECONDS + READINESS_TIMEOUT_SECONDS + EXECUTOR_SWITCH_TIMEOUT_SECONDS)
    + SWEEP_TIMEOUT_SECONDS
)
# The workflow has work before its bounded provisioning phase (checkout, uv,
# credential and image validation). Keep that reserve and a separate final job
# reserve outside the mega-brief runner so GitHub never kills pytest's finally.
STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS = 600
STAND_JOB_RESERVE_SECONDS = 480
# 360 minutes covers 45m provisioning + 10m workflow reserve + 297m maximum
# Product Brief runner + an 8m job reserve. The matrix path is smaller.
# Lifecycle cleanup has its own bounded workflow job because jobs cannot share
# one timeout.
STAND_JOB_TIMEOUT_MINUTES = 360
STAND_CLEANUP_JOB_TIMEOUT_MINUTES = 30
# The service that owns the decision, and is therefore the only one whose answer
# confirms a switch. Everything else reading QA_EXECUTOR_AGENT_TYPE is a consumer.
QA_EXECUTOR_RESOLVER_SERVICE = "api"
# The resolver's own answer, asked of the resolver. This is a dry run of exactly
# the call `POST /work-admission/paid-runs` makes (services/api/src/work_admission.py),
# in the process that will make it, and it creates no Run and writes nothing: what
# it prints is what the next paid QA Run is admitted under. Asking `qa-worker` for
# its local setting instead confirms only that the recreate this function just did
# happened — the defect run 33743251165 shipped.
#
# The break-glass global override is not applied here: it lives in the database,
# is set deliberately by an operator, and outranks this variable by design. A
# stand run under an active QA override is not switchable by `.env` at all.
_RESOLVED_EXECUTOR_SNIPPET = (
    "from shared.contracts.dto.run import RunType; "
    "from src.config import get_settings; "
    "from src.executor_resolver import resolve_executor_decision; "
    "print(resolve_executor_decision(RunType.QA, None, get_settings()).agent_type.value)"
)


@dataclass(frozen=True)
class Suite:
    """One named way to run the pipeline end to end."""

    target: str
    llm: bool
    #: Every (qa, worker) pair this suite runs. Empty means one run with whatever
    #: the caller asked for — or the defaults, for suites that use no agents.
    combinations: tuple[tuple[str, str], ...] = ()
    timeout_seconds: int = CUSTOM_TARGET_TIMEOUT_SECONDS
    #: Productive fixture cleanup is inside this additional window; the runner
    #: only becomes a group-killing backstop after both values are spent.
    cleanup_grace_seconds: int = 0
    #: A short process-group grace after the backstop interrupts a wedged suite.
    termination_grace_seconds: int = PROCESS_GROUP_TERMINATION_GRACE_SECONDS
    description: str = ""


SUITES: dict[str, Suite] = {
    "mega-noop": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipeline",
        llm=False,
        timeout_seconds=NOOP_SUITE_TIMEOUT_SECONDS,
        description="the full pipeline with a noop worker: infrastructure and deploy, no agents",
    ),
    "mega-llm": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipelineLLM",
        llm=True,
        timeout_seconds=LLM_SUITE_TIMEOUT_SECONDS,
        description="the full pipeline with a real coding agent and a real QA executor",
    ),
    "mega-brief": Suite(
        target="tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline",
        llm=True,
        timeout_seconds=BRIEF_SUITE_TIMEOUT_SECONDS,
        cleanup_grace_seconds=BRIEF_CLEANUP_GRACE_SECONDS,
        description=(
            "the confirmed Product Brief path with a real architect, coding agent, and QA executor"
        ),
    ),
    "matrix": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipelineLLM",
        llm=True,
        combinations=tuple((qa, worker) for qa in AGENTS for worker in AGENTS),
        timeout_seconds=LLM_SUITE_TIMEOUT_SECONDS,
        description="every QA executor against every worker agent",
    ),
}
SUITE_ALIASES = {"mega": "mega-noop", "llm": "mega-llm"}
LLM_ENV_NAMES = ("LIVE_LLM_QA", "LIVE_QA_AGENT_TYPE", "LIVE_WORKER_AGENT_TYPE")
LIVE_EVIDENCE_OUTPUT_DIR_ENV = "LIVE_EVIDENCE_OUTPUT_DIR"
LIVE_RELAY_LINE_MAX_CHARS = 4096


class PytestOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


def resolve_suite(requested_name: str) -> tuple[str, Suite]:
    """Resolve a legacy alias without letting it leak into public artifacts."""
    canonical_name = SUITE_ALIASES.get(requested_name, requested_name)
    suite = SUITES.get(canonical_name)
    if suite is not None:
        return canonical_name, suite
    return requested_name, Suite(target=requested_name, llm=False)


def read_env_file(path: Path) -> dict[str, str]:
    """Read the deployed .env the services run with."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value
    return values


def compose_environment(env: dict[str, str]) -> dict[str, str]:
    """The environment compose calls are made with.

    `QA_EXECUTOR_AGENT_TYPE` is removed on purpose: compose reads the process
    environment before the `.env` file, so leaving it here would pin the very
    value the caller is trying to change.
    """
    return {name: value for name, value in env.items() if name != QA_EXECUTOR_ENV}


def write_qa_executor(env_path: Path, executor: str) -> None:
    """Rewrite the deployed .env so the recreated container reads the new value."""
    kept = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{QA_EXECUTOR_ENV}=")
    ]
    kept.append(f"{QA_EXECUTOR_ENV}={executor}")
    env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def matrix_row(suite_name: str, qa: str, worker: str, status: str, seconds: int) -> str:
    return f"{suite_name}\t{qa}\t{worker}\t{status}\t{seconds}\n"


def write_junit_report(
    path: Path, suite_name: str, results: list[tuple[str, str, str, int]]
) -> None:
    """Write the stable JUnit companion for the human-readable TSV report.

    Pytest's own JUnit output is awkward for the matrix because each invocation
    would replace the previous file.  This report describes the runner's
    combinations instead: one deterministic testcase per requested pair.
    """
    failures = sum(status != "passed" for _qa, _worker, status, _seconds in results)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": f"stand-e2e:{suite_name}",
            "tests": str(len(results)),
            "failures": str(failures),
            "errors": "0",
        },
    )
    for qa, worker, status, seconds in results:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"stand-e2e:{suite_name}",
                "name": f"qa={qa} worker={worker}",
                "time": str(seconds),
            },
        )
        if status != "passed":
            ElementTree.SubElement(case, "failure", {"message": status})
    path.write_text(
        ElementTree.tostring(suite, encoding="unicode", short_empty_elements=True) + "\n",
        encoding="utf-8",
    )


def _compose(env: dict[str, str], *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    if args and args[0] in COMPOSE_LIFECYCLE_COMMANDS and not _INSIDE_RECREATE_GATE:
        raise RuntimeError(
            f"docker compose {args[0]!r} brings containers up; call recreate_and_wait() so the "
            "runner waits for what it started instead of racing it"
        )
    command = ["docker", "compose"]
    for name in COMPOSE_FILES:
        command += ["-f", name]
    command += list(args)
    return subprocess.run(  # noqa: S603
        command,
        cwd=REPO,
        env=compose_environment(env),
        capture_output=capture,
        text=True,
        check=False,
    )


def _mapping_entry(node: yaml.Node, key: str) -> yaml.Node | None:
    """The value node under `key`, or None when this is not a mapping with it."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for name, value in node.value:
        if isinstance(name, yaml.ScalarNode) and name.value == key:
            return value
    return None


def _environment_names(environment: yaml.Node | None) -> set[str]:
    """The variable names one compose `environment:` block declares, in either form."""
    if isinstance(environment, yaml.MappingNode):
        return {name.value for name, _ in environment.value if isinstance(name, yaml.ScalarNode)}
    if isinstance(environment, yaml.SequenceNode):
        return {
            entry.value.partition("=")[0].strip()
            for entry in environment.value
            if isinstance(entry, yaml.ScalarNode)
        }
    return set()


def qa_executor_services(root: Path = REPO) -> tuple[str, ...]:
    """Every compose service the QA executor variable is passed to.

    Derived from the compose files, never transcribed. The hand-written list this
    replaces named `qa-worker` alone and stayed correct-looking for six days while
    `api` — the service that actually decides — kept the value it had started with.
    A service that starts reading `QA_EXECUTOR_AGENT_TYPE` tomorrow is recreated by
    this function without anyone remembering it exists.

    Nodes are composed rather than constructed, for the same reason
    `scripts/check-ci-gate.py` does it: compose's own `!reset` in the production
    overlay has no constructor and would otherwise stop the parse. A service whose
    environment this cannot read outright — one assembled through `extends` or a
    YAML merge — is refused rather than walked past, because a service silently
    missed here is exactly the defect this function exists to end.
    """
    services: set[str] = set()
    for name in COMPOSE_FILES:
        path = root / name
        if not path.exists():
            continue
        for document in yaml.compose_all(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader):
            declared = _mapping_entry(document, "services")
            if not isinstance(declared, yaml.MappingNode):
                continue
            for service, definition in declared.value:
                service_name = (
                    service.value if isinstance(service, yaml.ScalarNode) else "<unnamed>"
                )
                for inherited in ("extends", "<<"):
                    if _mapping_entry(definition, inherited) is not None:
                        raise RuntimeError(
                            f"{name}: service {service_name} assembles its environment through "
                            f"{inherited!r}, which this derivation does not follow; declare "
                            f"{QA_EXECUTOR_ENV} on the service itself"
                        )
                if QA_EXECUTOR_ENV in _environment_names(_mapping_entry(definition, "environment")):
                    services.add(service_name)
    if not services:
        raise RuntimeError(
            f"no compose service in {', '.join(COMPOSE_FILES)} is given {QA_EXECUTOR_ENV}; "
            "the stand runner cannot switch a QA executor nothing reads"
        )
    return tuple(sorted(services))


def resolved_qa_executor(env: dict[str, str]) -> str | None:
    """Ask the resolver which executor a QA Run would now be admitted under.

    This is the decision itself, not a proxy for it: the same
    `resolve_executor_decision` call paid-run admission makes, evaluated in the
    `api` process whose settings are its input. A consumer's local setting answers
    a different question — what this container was told — and answering that one
    is how a run asked for Claude and spent Codex.
    """
    result = _compose(
        env,
        "exec",
        "-T",
        QA_EXECUTOR_RESOLVER_SERVICE,
        "python",
        "-c",
        _RESOLVED_EXECUTOR_SNIPPET,
        capture=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def api_serves_health() -> bool:
    """Does the API answer `GET /health` with 200 where the suite will ask?

    From this process, over the network `tests/live/conftest.py` uses. The
    container answering its own `curl` is a different fact, and it is the one
    that was true at 11:30:08 on run 33749154999 while the suite's first request
    was failing with `httpx.ReadError`.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 — a fixed loopback http URL
            SUITE_API_BASE_URL + API_HEALTH_PATH, timeout=READINESS_POLL_SECONDS
        ) as response:
            return response.status == API_HEALTH_OK_STATUS
    except OSError:  # HTTPError is one, so a 503 answers False like a refused connection
        return False


def consumer_past_startup(env: dict[str, str], service: str) -> bool:
    """Has a queue consumer finished starting, rather than merely being up?

    `run_queue_worker` (services/langgraph/src/consumers/_base.py) logs
    `<service>_started` once, after it has connected to Redis and read its slot
    configuration — that is, once it is actually reading its queue. Before that
    line the container is running and the work the suite queues would sit
    unclaimed. The recreate removed the previous container, so these logs belong
    to the one just started.
    """
    result = _compose(env, "logs", "--no-color", service, capture=True)
    return result.returncode == 0 and f"{service}_started" in result.stdout


def service_is_ready(env: dict[str, str], service: str) -> bool:
    """Is this service usable the way the suite will use it?

    `api` is used over HTTP, so it is asked over HTTP. Everything else the
    recreate set can name is a queue consumer of `run_queue_worker`, and is ready
    when it says it started. A service that is neither never reports ready, so the
    gate times out and the cell is refused — the runner's failure mode here is to
    stop, never to proceed on an unready stack.
    """
    if service == QA_EXECUTOR_RESOLVER_SERVICE:
        return api_serves_health()
    return consumer_past_startup(env, service)


@contextmanager
def _recreate_gate() -> Iterator[None]:
    global _INSIDE_RECREATE_GATE
    _INSIDE_RECREATE_GATE = True
    try:
        yield
    finally:
        _INSIDE_RECREATE_GATE = False


def recreate_and_wait(env: dict[str, str], services: tuple[str, ...], log) -> bool:
    """Force-recreate services and return only once the suite could use them.

    The runner's one way to bring a container up: `_compose` refuses `up`,
    `start` and `restart` anywhere else. Recreating and waiting are therefore one
    operation, and a caller who needs the first half inherits the second instead
    of having to remember it — which is what run 33749154999 cost. There the
    recreate returned, the resolver answered from the new `api` process as soon
    as its Python could import, and pytest started 4 seconds before uvicorn
    listened.
    """
    with _recreate_gate():
        _compose(env, "up", "-d", "--no-deps", "--force-recreate", *services, capture=True)

    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    for service in services:
        while not service_is_ready(env, service):
            if time.monotonic() >= deadline:
                log(
                    f"{service} was not usable {READINESS_TIMEOUT_SECONDS}s after being "
                    f"recreated with {', '.join(services)}"
                )
                return False
            time.sleep(READINESS_POLL_SECONDS)
    return True


def ensure_qa_executor(env: dict[str, str], executor: str, log) -> bool:
    """Switch the QA executor everywhere, and wait for the resolver to say so.

    Recreate, then readiness as the suite reaches it, then the resolver's own
    answer, then — and only then — the cell. A timeout at either wait returns
    `False`, which the caller records as `qa_executor_switch_failed` and skips.
    """
    if resolved_qa_executor(env) == executor:
        return True

    write_qa_executor(REPO / ".env", executor)
    services = qa_executor_services()
    if not recreate_and_wait(env, services, log):
        return False

    deadline = time.monotonic() + EXECUTOR_SWITCH_TIMEOUT_SECONDS
    while True:
        answer = resolved_qa_executor(env)
        if answer == executor:
            return True
        if time.monotonic() >= deadline:
            log(
                f"the resolver never answered {executor!r} after recreating "
                f"{', '.join(services)} (last answer {answer!r})"
            )
            return False
        time.sleep(5)


def run_pytest(
    target: str,
    env: dict[str, str],
    extra: dict[str, str],
    log_path: Path,
    timeout_seconds: int,
    termination_grace_seconds: int = PROCESS_GROUP_TERMINATION_GRACE_SECONDS,
    log=print,
) -> PytestOutcome:
    run_env = {
        **os.environ,
        **env,
        "LIVE_CONTOUR": "stand",
        # The orchestrator host is disposable. Evidence written under the
        # checkout disappears with it, so make the runner-owned directory the
        # durable handoff source while the fixture still owns its containers.
        LIVE_EVIDENCE_OUTPUT_DIR_ENV: str(log_path.parent),
    }
    for name in LLM_ENV_NAMES:
        run_env.pop(name, None)
    run_env.update(extra)
    secrets = secret_env_values(run_env)
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(  # noqa: S603
            ["uv", "run", "pytest", target, "-x", "-q", "-s", "--tb=short"],  # noqa: S607
            cwd=REPO,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        def mirror() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                safe_line = redact_diagnostic(line.rstrip("\n"), secrets=secrets)
                if len(safe_line) > LIVE_RELAY_LINE_MAX_CHARS:
                    safe_line = f"{safe_line[: LIVE_RELAY_LINE_MAX_CHARS - 1]}…"
                handle.write(f"{safe_line}\n")
                handle.flush()
                log(safe_line)

        output = Thread(target=mirror, daemon=True)
        output.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            log(
                "pytest hard deadline exhausted after "
                f"{timeout_seconds}s; interrupting process group"
            )
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=termination_grace_seconds)
            except subprocess.TimeoutExpired:
                log(
                    "pytest process-group termination grace exhausted after "
                    f"{termination_grace_seconds}s; killing process group"
                )
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            output.join(timeout=RELAY_JOIN_TIMEOUT_SECONDS)
            if output.is_alive():
                log(f"pytest output relay remained open after {RELAY_JOIN_TIMEOUT_SECONDS}s")
                if process.stdout is not None:
                    process.stdout.close()
                output.join(timeout=RELAY_JOIN_TIMEOUT_SECONDS)
    if timed_out:
        return PytestOutcome.TIMED_OUT
    return PytestOutcome.PASSED if process.returncode == 0 else PytestOutcome.FAILED


def preflight(env: dict[str, str], log) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            # As a module, not a path: running `python scripts/x.py` puts `scripts/`
            # on sys.path instead of the repository root, and the script cannot then
            # import `shared`. That has refused a run twice.
            [sys.executable, "-m", "scripts.stand_preflight"],  # noqa: S607
            cwd=REPO,
            env={**os.environ, **env, "LIVE_CONTOUR": "stand"},
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log(f"preflight timed out after {PREFLIGHT_TIMEOUT_SECONDS}s")
        return False
    for line in result.stdout.splitlines():
        log(line)
    return result.returncode == 0


def sweep(env: dict[str, str], log) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            # As a module: a path invocation puts `scripts/` on sys.path instead
            # of the repository root, and `shared` then cannot be imported.
            ["uv", "run", "python", "-m", "scripts.clean_live_tests"],  # noqa: S607
            cwd=REPO,
            env={**os.environ, **env, "LIVE_CONTOUR": "stand"},
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log(f"sweep timed out after {SWEEP_TIMEOUT_SECONDS}s")
        return False
    if result.returncode != 0:
        log(f"sweep failed: {(result.stderr or result.stdout).strip().splitlines()[-1:]}")
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an e2e suite on the stand.",
        epilog=(
            "suites: "
            + "; ".join(f"{name} — {s.description}" for name, s in SUITES.items())
            + "; legacy aliases: mega → mega-noop, llm → mega-llm"
        ),
    )
    parser.add_argument("--suite", required=True, help="a named suite, or any pytest target")
    parser.add_argument("--worker", choices=AGENTS, default="claude")
    parser.add_argument("--qa", choices=AGENTS, default="codex")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-sweep", action="store_true", help="leave resources for inspection")
    args = parser.parse_args()

    canonical_suite_name, suite = resolve_suite(args.suite)
    env = read_env_file(REPO / ".env")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUN_ROOT / f"{canonical_suite_name.replace('/', '_')}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    latest = RUN_ROOT / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(run_dir)
    journal = run_dir / "run.log"

    def log(message: str) -> None:
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}\n")
        print(message, flush=True)

    log(
        f"suite={canonical_suite_name} requested_suite={args.suite} "
        f"target={suite.target} productive_timeout_seconds={suite.timeout_seconds} "
        f"hard_timeout_seconds={suite.timeout_seconds + suite.cleanup_grace_seconds} dir={run_dir}"
    )

    report = run_dir / "report.tsv"
    report.write_text("suite\tqa_agent\tworker_agent\tstatus\tduration_seconds\n", encoding="utf-8")
    results: list[tuple[str, str, str, int]] = []

    if not args.skip_preflight and not preflight(env, log):
        log("preflight refused the run")
        results.append((args.qa, args.worker, "preflight_failed", 0))
        with report.open("a", encoding="utf-8") as handle:
            handle.write(matrix_row(canonical_suite_name, *results[-1]))
        write_junit_report(run_dir / "junit.xml", canonical_suite_name, results)
        return 2

    combinations = suite.combinations or ((args.qa, args.worker),)

    failed = 0
    for qa, worker in combinations:
        extra: dict[str, str] = {}
        if suite.llm:
            if not ensure_qa_executor(env, qa, log):
                with report.open("a", encoding="utf-8") as handle:
                    handle.write(
                        matrix_row(canonical_suite_name, qa, worker, "qa_executor_switch_failed", 0)
                    )
                results.append((qa, worker, "qa_executor_switch_failed", 0))
                failed += 1
                continue
            extra = {
                "LIVE_LLM_QA": "1",
                "LIVE_QA_AGENT_TYPE": qa,
                "LIVE_WORKER_AGENT_TYPE": worker,
            }

        started = time.monotonic()
        log(f"running qa={qa} worker={worker}")
        outcome = run_pytest(
            suite.target,
            env,
            extra,
            run_dir / f"{qa}-{worker}.log",
            suite.timeout_seconds + suite.cleanup_grace_seconds,
            suite.termination_grace_seconds,
            log,
        )
        # Test seams from before typed runner outcomes returned booleans. Keep
        # those narrow fakes readable while production always names a result.
        if isinstance(outcome, bool):
            outcome = PytestOutcome.PASSED if outcome else PytestOutcome.FAILED
        seconds = round(time.monotonic() - started)
        with report.open("a", encoding="utf-8") as handle:
            handle.write(matrix_row(canonical_suite_name, qa, worker, outcome.value, seconds))
        results.append((qa, worker, outcome.value, seconds))
        log(f"qa={qa} worker={worker}: {outcome.value} in {seconds}s")
        if outcome is not PytestOutcome.PASSED:
            failed += 1

    # Cleanup is part of the result, not an epilogue. A sweep that failed leaves
    # database rows, GitHub repositories, workers and workspaces behind for the
    # next serialized run to inherit, and reporting green over that hides the
    # residue until it breaks something else.
    swept = args.skip_sweep or sweep(env, log)

    write_junit_report(run_dir / "junit.xml", canonical_suite_name, results)
    log(report.read_text(encoding="utf-8").rstrip())
    if not swept:
        log("cleanup failed; the run is not green regardless of the suite result")
    return 1 if failed or not swept else 0


if __name__ == "__main__":
    raise SystemExit(main())
