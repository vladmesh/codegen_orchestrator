"""The operator's commands for one synthetic-buyer operation.

    python -m src.synthetic_buyer check   --config buyer.json
    python -m src.synthetic_buyer run     --config buyer.json --orchestrator-revision <sha>
    python -m src.synthetic_buyer resume  --config buyer.json --orchestrator-revision <sha>
    python -m src.synthetic_buyer cleanup --config buyer.json --orchestrator-revision <sha>
    python -m src.synthetic_buyer inspect --config buyer.json

`check` and `inspect` are offline: they read the config, the presence of each
secret handle (never its value) and the retained evidence, and connect to
nothing. `run` starts a new operation and refuses one whose evidence exists;
`resume` continues an interrupted one from its retained ids; `cleanup` only tears
down the project the retained evidence names. Nothing here is triggered by CI.
See docs/runbooks/synthetic-buyer.md.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import os
from pathlib import Path
import re
import sys

import structlog

from shared.log_config import setup_logging

from .config import BuyerConfig, ConfigError, MissingSecret, load_config, resolve_secret
from .evidence import EvidenceStore, new_record
from .live import INTERNAL_API_KEY_ENV, RUNTIME_KEY_ENV, live_buyer, new_store

EXIT_CONFIG = 2
EXIT_REFUSED = 3
logger = structlog.get_logger()


def handle_presence(config: BuyerConfig, environ: Mapping[str, str]) -> dict[str, bool]:
    """Which handles resolve to a non-empty value, by role. Values never leave this call."""
    presence = {}
    for role, handle in config.secret_handles().items():
        try:
            resolve_secret(handle, environ)
        except MissingSecret:
            presence[role] = False
        else:
            presence[role] = True
    for name in (INTERNAL_API_KEY_ENV, RUNTIME_KEY_ENV):
        presence[f"runtime.{name}"] = bool(environ.get(name, "").strip())
    return presence


def check(config: BuyerConfig, environ: Mapping[str, str]) -> tuple[int, dict]:
    presence = handle_presence(config, environ)
    store = new_store(config)
    summary = {
        "operation_id": config.operation_id,
        "codegen_bot": f"@{config.codegen_bot.username}",
        "buyer_telegram_id": config.buyer.telegram_id,
        "public_channels": config.scenario.public_channels,
        "model_chain": [entry.model_dump(mode="json") for entry in config.model.chain],
        "handles": {role: handle.describe() for role, handle in config.secret_handles().items()},
        "present": presence,
        "evidence": str(store.path),
        "evidence_exists": store.exists(),
    }
    if store.exists():
        record = store.load()
        summary.update(
            phase=record["phase"],
            verdict=record["verdict"]["status"],
            cleanup=record["cleanup"]["status"],
        )
    return (0 if all(presence.values()) else 1), summary


def _revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("the orchestrator revision is a 40-hex commit")
    return value


async def operate(
    command: str, config: BuyerConfig, revision: str, environ: Mapping[str, str]
) -> int:
    store = new_store(config)
    missing = sorted(
        role for role, present in handle_presence(config, environ).items() if not present
    )
    if missing:
        logger.error("synthetic_buyer_refused", reason="handles_missing", roles=missing)
        return EXIT_CONFIG
    if command == "run":
        if store.exists():
            logger.error("synthetic_buyer_refused", reason="evidence_exists", hint="use resume")
            return EXIT_REFUSED
        store.record = new_record(
            operation_id=config.operation_id,
            revision=revision,
            handles={role: handle.describe() for role, handle in config.secret_handles().items()},
            now=store.now(),
        )
        store.save()
    else:
        if not store.exists():
            logger.error("synthetic_buyer_refused", reason="no_retained_evidence")
            return EXIT_REFUSED
        record = store.load()
        if record["operation_id"] != config.operation_id:
            logger.error("synthetic_buyer_refused", reason="operation_mismatch")
            return EXIT_REFUSED
        record.setdefault("resumed_with_revisions", []).append(
            {"command": command, "revision": revision, "at": store.now()}
        )
        store.save()
    try:
        async with live_buyer(config, store, environ) as buyer:
            if command == "cleanup":
                cleanup = await buyer.cleanup()
                logger.info("synthetic_buyer_cleanup", cleanup=cleanup, evidence=str(store.path))
                return 0 if cleanup in {"completed", "nothing_owned"} else 1
            outcome = await buyer.run()
    except Exception as error:  # noqa: BLE001 - a construction error may quote its inputs
        logger.error("synthetic_buyer_not_started", error_type=type(error).__name__)
        return 1
    logger.info(
        "synthetic_buyer_outcome",
        verdict=outcome.verdict,
        cleanup=outcome.cleanup,
        evidence=str(store.path),
    )
    return outcome.exit_code


def inspect(config: BuyerConfig) -> int:
    store: EvidenceStore = new_store(config)
    if not store.exists():
        logger.error("synthetic_buyer_refused", reason="no_retained_evidence")
        return EXIT_REFUSED
    record = store.load()
    logger.info(
        "synthetic_buyer_inspect",
        phase=record["phase"],
        completed=record["completed_phases"],
        verdict=record["verdict"],
        cleanup=record["cleanup"],
        ids=record["ids"],
        report=str(store.directory / "report.md"),
    )
    return 0


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    setup_logging(service_name="synthetic_buyer")
    parser = argparse.ArgumentParser(prog="python -m src.synthetic_buyer")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "inspect"):
        commands.add_parser(name).add_argument("--config", type=Path, required=True)
    for name in ("run", "resume", "cleanup"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--orchestrator-revision", type=_revision, required=True)
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        config = load_config(args.config)
    except ConfigError as error:
        logger.error("synthetic_buyer_config_refused", detail=str(error))
        return EXIT_CONFIG
    if args.command == "check":
        code, summary = check(config, environ)
        logger.info("synthetic_buyer_check", **summary)
        return code
    if args.command == "inspect":
        return inspect(config)
    try:
        return asyncio.run(operate(args.command, config, args.orchestrator_revision, environ))
    except MissingSecret as error:
        logger.error("synthetic_buyer_secret_missing", detail=str(error))
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
