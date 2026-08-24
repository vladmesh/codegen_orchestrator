"""Bounded, redacted worker observability artifacts."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

import structlog

from shared.contracts.dto.engineering_attempt import ClaudeResultEvidence, FactoryResultEvidence
from shared.diagnostics import redact_diagnostic

_SECRET_NAME = re.compile(r"(?:key|secret|token|password|credential|authorization)", re.I)
logger = structlog.get_logger(__name__)


def redact_transcript(text: str, environment: dict[str, str]) -> str:
    """Apply the diagnostic redaction policy plus values carried in secret env vars."""
    secrets = [value for name, value in environment.items() if value and _SECRET_NAME.search(name)]
    return redact_diagnostic(text, secrets=secrets)


def extract_effort_metrics(stdout: str, stderr: str, agent_type: str) -> dict[str, Any]:
    """Return provider-reported usage, leaving unavailable values absent.

    Claude's JSON result has an exact-cost evidence contract. Factory accepts
    only one whole ``type=result`` JSON document, retaining its non-negative
    model and usage facts without interpreting money. Codex's non-interactive
    output has no stable usage contract, so it intentionally returns no facts.
    """
    if agent_type == "codex":
        return {}
    if agent_type == "claude":
        return _extract_claude_evidence(stdout)
    if agent_type == "factory":
        return _extract_factory_evidence(stdout)
    return {}


def _extract_factory_evidence(stdout: str) -> dict[str, Any]:
    """Parse only one Factory final-result JSON document.

    ``droid exec -o json`` produces one object.  JSONL, leading diagnostics,
    and non-result objects are deliberately unavailable rather than sources to
    combine.  Factory money-looking fields are outside this evidence boundary.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return {}
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    try:
        evidence = FactoryResultEvidence(
            model=_factory_model(payload),
            input_tokens=_nonnegative_int(usage.get("input_tokens")),
            output_tokens=_nonnegative_int(usage.get("output_tokens")),
            total_tokens=_nonnegative_int(usage.get("total_tokens")),
            cache_read_tokens=_nonnegative_int(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_nonnegative_int(usage.get("cache_creation_input_tokens")),
        )
    except ValueError:
        return {}
    if not any(
        getattr(evidence, field) is not None
        for field in (
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    ):
        return {}
    return {"factory_evidence": evidence}


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
    evidence = ClaudeResultEvidence(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        cache_read_tokens=_nonnegative_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_nonnegative_int(usage.get("cache_creation_input_tokens")),
        cost_microusd=_micro_usd(payload.get("total_cost_usd")),
    )
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


def _factory_model(payload: dict[str, Any]) -> str | None:
    """Keep a reported Factory model only when it satisfies its evidence type."""
    model = payload.get("model")
    return model if isinstance(model, str) and model else None


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
