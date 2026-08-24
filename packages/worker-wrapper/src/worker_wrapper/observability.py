"""Bounded, redacted worker observability artifacts."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

import structlog

from shared.diagnostics import redact_diagnostic

_SECRET_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)
logger = structlog.get_logger(__name__)


def redact_transcript(text: str, environment: dict[str, str]) -> str:
    """Apply the diagnostic redaction policy plus values carried in secret env vars."""
    secrets = [value for name, value in environment.items() if value and _SECRET_NAME.search(name)]
    return redact_diagnostic(text, secrets=secrets)


def extract_effort_metrics(stdout: str, stderr: str, agent_type: str) -> dict[str, Any]:
    """Return provider-reported usage, leaving unavailable values absent.

    Claude's JSON result exposes ``usage`` and ``total_cost_usd``. Factory may
    expose the same keys in its JSON output. Codex's non-interactive output in
    the pinned CLI has no stable usage contract, so it intentionally returns no
    fabricated metrics.
    """
    if agent_type == "codex":
        return {}
    if agent_type == "claude":
        return _extract_claude_evidence(stdout)
    payloads: list[dict[str, Any]] = []
    # Claude --output-format json emits an indented JSON document. Parse it as
    # a whole first; line-by-line parsing is only for JSONL-style adapters.
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        payloads.append(value)
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    for payload in reversed(payloads):
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if (
            total_tokens is None
            and isinstance(input_tokens, int)
            and isinstance(output_tokens, int)
        ):
            total_tokens = input_tokens + output_tokens
        result = {
            key: value
            for key, value in {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_usd": payload.get("total_cost_usd", payload.get("cost_usd")),
            }.items()
            if value is not None
        }
        if result:
            return result
    return {}


def _extract_claude_evidence(stdout: str) -> dict[str, Any]:
    """Parse exactly one documented Claude final-result JSON object.

    Claude emits one JSON document, rather than JSONL. Parsing it as a whole
    ensures a cost from one record cannot be paired with usage from another.
    ``parse_float=Decimal`` is required before converting USD to micro-USD.
    """
    try:
        payload = json.loads(stdout, parse_float=Decimal)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return {}

    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    model = _claude_model(payload)
    evidence = {
        "provider": "anthropic",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        "cache_read_tokens": _nonnegative_int(usage.get("cache_read_input_tokens")),
        "cache_write_tokens": _nonnegative_int(usage.get("cache_creation_input_tokens")),
        "cost_microusd": _micro_usd(payload.get("total_cost_usd")),
    }
    return {"claude_evidence": evidence}


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _claude_model(payload: dict[str, Any]) -> str | None:
    model = payload.get("model")
    if isinstance(model, str):
        return model
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict):
        return None
    models = [name for name in model_usage if isinstance(name, str)]
    return models[0] if len(models) == 1 else None


def _micro_usd(value: Any) -> int | None:
    """Round a valid Decimal provider amount to the ledger's integer unit."""
    if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
        return None
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * Decimal("1000000")).to_integral_value(rounding=ROUND_HALF_UP))


def save_transcript(
    directory: str,
    worker_id: str,
    request_id: str,
    content: str,
    max_bytes: int,
    environment: dict[str, str],
) -> tuple[str | None, bool]:
    """Persist a bounded redacted artifact. Artifact failures are non-fatal."""
    try:
        encoded = redact_transcript(content, environment).encode("utf-8", errors="replace")
        truncated = len(encoded) > max_bytes
        if truncated:
            marker = b"\n\n[transcript truncated at configured limit]\n"
            prefix_limit = max(0, max_bytes - len(marker))
            # Do not split a UTF-8 code point when retaining the prefix.
            encoded = (
                encoded[:prefix_limit].decode("utf-8", errors="ignore").encode("utf-8") + marker
            )
        path = Path(directory) / worker_id
        path.mkdir(parents=True, exist_ok=True)
        artifact = path / f"{request_id}.log"
        artifact.write_bytes(encoded)
        return str(artifact), truncated
    except OSError as exc:
        logger.warning("transcript_save_failed", error_type=type(exc).__name__)
        return None, False
