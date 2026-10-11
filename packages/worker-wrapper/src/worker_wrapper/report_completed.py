"""Native developer completion producer. The agent supplies only its summary."""

import argparse
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
import subprocess

import structlog

from shared.diagnostics import redact_diagnostic
from shared.log_config import setup_logging

from .http_models import ResultRequest

logger = structlog.get_logger(__name__)


def completed_payload(summary: str, workspace: Path = Path("/workspace")) -> bytes:
    commit = subprocess.run(  # noqa: S603 - fixed native Git command, no shell
        [
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    return (
        ResultRequest(success=True, commit=commit, summary=summary)
        .model_dump_json(exclude_none=True)
        .encode()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()
    setup_logging(service_name="worker_report_completed")
    connection = None
    try:
        payload = completed_payload(args.summary)
        connection = HTTPConnection("127.0.0.1", 9090, timeout=180)
        connection.request(
            "POST",
            "/result",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        logger.info(
            "completion_response",
            status=response.status,
            evidence=redact_diagnostic(response.read(8192).decode(errors="replace"))[:2000],
        )
        code = 0 if response.status == HTTPStatus.OK else 1
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.error("completion_submission_refused", error=redact_diagnostic(str(exc))[:2000])
        code = 1
    finally:
        if connection is not None:
            connection.close()
    raise SystemExit(code)
