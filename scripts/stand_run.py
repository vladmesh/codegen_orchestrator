#!/usr/bin/env python3
"""Run an e2e suite on the stand: one entry point, one place that knows how.

Everything here was a hand-written shell script once, and each of them learned
the same lessons separately and then died with the session that wrote it. The
lessons are the reason this file exists:

* **The QA executor is a property of a running container.** Switching it means
  rewriting `.env` and recreating `qa-worker` — and then *waiting* for the new
  container, because the old one keeps answering for a few seconds and will
  happily report the executor you just replaced.

* **An exported variable outranks `.env`.** Compose interpolates
  `${QA_EXECUTOR_AGENT_TYPE:-codex}` from the process environment first, so a
  runner that sourced `.env` into its own environment pins the executor it is
  trying to change. Every compose call here is made with that name removed.

* **A run outlives the SSH session that starts it.** A mega takes ten minutes,
  a matrix an hour; both are longer than a connection reliably lives. Start this
  detached (`setsid nohup`) and read the log it names.

Suites are a table, not code paths, so a new one is a line:

    ./scripts/stand_run.py --suite mega
    ./scripts/stand_run.py --suite llm --worker codex --qa claude
    ./scripts/stand_run.py --suite matrix
    ./scripts/stand_run.py --suite tests/live/test_api_crud.py
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.stand.yml")
QA_EXECUTOR_ENV = "QA_EXECUTOR_AGENT_TYPE"
AGENTS = ("claude", "codex")
RUN_ROOT = Path.home() / "e2e-runs"
EXECUTOR_SWITCH_TIMEOUT_SECONDS = 180
SUITE_TIMEOUT_SECONDS = 2700
# Asked of the container rather than of .env: what matters is the executor the
# running qa-worker resolved, not the value someone wrote.
_ACTIVE_EXECUTOR_SNIPPET = (
    "from src.config.settings import get_settings; "
    "print(get_settings().qa_executor_agent_type.value)"
)


@dataclass(frozen=True)
class Suite:
    """One named way to run the pipeline end to end."""

    target: str
    llm: bool
    #: Every (qa, worker) pair this suite runs. Empty means one run with whatever
    #: the caller asked for — or the defaults, for suites that use no agents.
    combinations: tuple[tuple[str, str], ...] = ()
    description: str = ""


SUITES: dict[str, Suite] = {
    "mega": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipeline",
        llm=False,
        description="the full pipeline with a noop worker: infrastructure and deploy, no agents",
    ),
    "llm": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipelineLLM",
        llm=True,
        description="the full pipeline with a real coding agent and a real QA executor",
    ),
    "matrix": Suite(
        target="tests/live/test_full_pipeline.py::TestFullPipelineLLM",
        llm=True,
        combinations=tuple((qa, worker) for qa in AGENTS for worker in AGENTS),
        description="every QA executor against every worker agent",
    ),
}


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


def matrix_row(qa: str, worker: str, status: str, seconds: int) -> str:
    return f"{qa}\t{worker}\t{status}\t{seconds}\n"


def _compose(env: dict[str, str], *args: str, capture: bool = False) -> subprocess.CompletedProcess:
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


def active_qa_executor(env: dict[str, str]) -> str | None:
    result = _compose(
        env,
        "exec",
        "-T",
        "qa-worker",
        "python",
        "-c",
        _ACTIVE_EXECUTOR_SNIPPET,
        capture=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ensure_qa_executor(env: dict[str, str], executor: str, log) -> bool:
    """Switch the QA executor and wait until the new container confirms it."""
    if active_qa_executor(env) == executor:
        return True

    write_qa_executor(REPO / ".env", executor)
    _compose(env, "up", "-d", "--no-deps", "--force-recreate", "qa-worker", capture=True)

    deadline = time.monotonic() + EXECUTOR_SWITCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        actual = active_qa_executor(env)
        if actual == executor:
            return True
        time.sleep(5)
    log(f"qa executor never became {executor!r} (last seen {active_qa_executor(env)!r})")
    return False


def run_pytest(target: str, env: dict[str, str], extra: dict[str, str], log_path: Path) -> bool:
    run_env = {**os.environ, **env, **extra, "LIVE_CONTOUR": "stand"}
    with log_path.open("w", encoding="utf-8") as handle:
        result = subprocess.run(  # noqa: S603
            ["uv", "run", "pytest", target, "-x", "-q", "--tb=short"],  # noqa: S607
            cwd=REPO,
            env=run_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=SUITE_TIMEOUT_SECONDS,
            check=False,
        )
    return result.returncode == 0


def preflight(env: dict[str, str], log) -> bool:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "stand_preflight.py")],  # noqa: S607
        cwd=REPO,
        env={**os.environ, **env, "LIVE_CONTOUR": "stand"},
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        log(line)
    return result.returncode == 0


def sweep(env: dict[str, str], log) -> bool:
    result = subprocess.run(  # noqa: S603
        ["uv", "run", "python", str(REPO / "scripts" / "clean_live_tests.py")],  # noqa: S607
        cwd=REPO,
        env={**os.environ, **env, "LIVE_CONTOUR": "stand"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(f"sweep failed: {(result.stderr or result.stdout).strip().splitlines()[-1:]}")
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an e2e suite on the stand.",
        epilog="suites: " + "; ".join(f"{name} — {s.description}" for name, s in SUITES.items()),
    )
    parser.add_argument("--suite", required=True, help="a named suite, or any pytest target")
    parser.add_argument("--worker", choices=AGENTS, default="claude")
    parser.add_argument("--qa", choices=AGENTS, default="codex")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-sweep", action="store_true", help="leave resources for inspection")
    args = parser.parse_args()

    suite = SUITES.get(args.suite) or Suite(target=args.suite, llm=False)
    env = read_env_file(REPO / ".env")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUN_ROOT / f"{args.suite.replace('/', '_')}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    latest = RUN_ROOT / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(run_dir)
    journal = run_dir / "run.log"

    def log(message: str) -> None:
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}\n")
        print(message, flush=True)

    log(f"suite={args.suite} target={suite.target} dir={run_dir}")

    if not args.skip_preflight and not preflight(env, log):
        log("preflight refused the run")
        return 2

    combinations = suite.combinations or ((args.qa, args.worker),)
    report = run_dir / "report.tsv"
    report.write_text("qa_agent\tworker_agent\tstatus\tduration_seconds\n", encoding="utf-8")

    failed = 0
    for qa, worker in combinations:
        extra: dict[str, str] = {}
        if suite.llm:
            if not ensure_qa_executor(env, qa, log):
                with report.open("a", encoding="utf-8") as handle:
                    handle.write(matrix_row(qa, worker, "qa_executor_switch_failed", 0))
                failed += 1
                continue
            extra = {
                "LIVE_LLM_QA": "1",
                "LIVE_QA_AGENT_TYPE": qa,
                "LIVE_WORKER_AGENT_TYPE": worker,
            }

        started = time.monotonic()
        log(f"running qa={qa} worker={worker}")
        passed = run_pytest(suite.target, env, extra, run_dir / f"{qa}-{worker}.log")
        seconds = round(time.monotonic() - started)
        with report.open("a", encoding="utf-8") as handle:
            handle.write(matrix_row(qa, worker, "passed" if passed else "failed", seconds))
        log(f"qa={qa} worker={worker}: {'passed' if passed else 'failed'} in {seconds}s")
        if not passed:
            failed += 1

    if not args.skip_sweep:
        sweep(env, log)

    log(report.read_text(encoding="utf-8").rstrip())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
