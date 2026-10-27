"""The QA executor's result contract: one validator for the endpoint and the runner."""

from __future__ import annotations

import json
import re

from shared.contracts.dto.run_result import QAFailedCheckCause

_REQUIRED_FIELDS = frozenset({"pass", "checks", "summary"})
# Cleanup evidence is runner-owned, not trusted agent output.
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"state_changes"}
_PASSED_CHECK_FIELDS = frozenset({"name", "pass", "detail"})
_FAILED_CHECK_FIELDS = _PASSED_CHECK_FIELDS | {"cause"}
#: A check the transport could not carry: no `pass`, no `cause`. It counts toward
#: nothing and is kept only when the runtime recorded the refusal itself.
_NOT_APPLICABLE_CHECK_FIELDS = frozenset({"name", "not_applicable", "detail"})


class QAResultInvalid(ValueError):
    """The executor's answer cannot safely drive QA routing; its text names the broken rule."""


def decode_qa_result(raw: str) -> dict:
    """Return the result object of a raw, fenced or result-wrapped answer.

    Raises `QAResultInvalid` naming the first rule of the result contract the answer breaks.
    """
    json_str = raw.strip()
    try:
        wrapper = json.loads(json_str)
        if isinstance(wrapper, dict) and wrapper.get("type") == "result":
            json_str = wrapper.get("result", "")
    except json.JSONDecodeError:
        pass  # Not a wrapper, continue with raw

    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise QAResultInvalid(f"the result is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QAResultInvalid("the result is not a JSON object")
    reason = _validate_qa_payload(data)
    if reason:
        raise QAResultInvalid(reason)
    return data


def _top_level_error(data: dict) -> str | None:
    unexpected = sorted(set(data) - _ALLOWED_FIELDS)
    missing = sorted(_REQUIRED_FIELDS - set(data))
    if not unexpected and not missing:
        return None
    named = []
    if unexpected:
        named.append("unexpected top-level keys: " + ", ".join(unexpected))
    if missing:
        named.append("missing top-level keys: " + ", ".join(missing))
    return "expected exactly pass, checks, and summary fields; " + "; ".join(named)


def _check_shape_error(index: int, check: object, causes: set[str]) -> str | None:
    """Name what is wrong with one executor check, or None when its shape is valid."""
    if isinstance(check, dict) and "telegram_step" in check:
        if type(check["telegram_step"]) is not int or check["telegram_step"] < 1:
            return f"check {index} telegram_step must be a positive integer"
        check = {key: value for key, value in check.items() if key != "telegram_step"}
    if isinstance(check, dict) and "not_applicable" in check:
        if check["not_applicable"] is not True or set(check) != _NOT_APPLICABLE_CHECK_FIELDS:
            return (
                f"not-applicable check {index} must contain exactly name, "
                "not_applicable (true), and detail fields"
            )
    elif not isinstance(check, dict) or not isinstance(check.get("pass"), bool):
        return f"check {index} pass must be a boolean"
    elif check["pass"] and set(check) != _PASSED_CHECK_FIELDS:
        return f"passed check {index} must contain exactly name, pass, and detail fields"
    elif not check["pass"] and set(check) != _FAILED_CHECK_FIELDS:
        return f"failed check {index} must contain exactly name, pass, detail, and cause fields"
    if not isinstance(check["name"], str) or not check["name"].strip():
        return f"check {index} name must be a non-empty string"
    if not isinstance(check["detail"], str) or not check["detail"].strip():
        return f"check {index} detail must be a non-empty string"
    if check.get("pass") is False and check["cause"] not in causes:
        return f"failed check {index} cause must be one of {', '.join(sorted(causes))}"
    return None


def _validate_qa_payload(data: dict) -> str | None:
    """Name the first routing-relevant field that is invalid, or None when all are valid."""
    top_level_error = _top_level_error(data)
    if top_level_error:
        return top_level_error

    if not isinstance(data["pass"], bool):
        return "pass must be a boolean"
    if not isinstance(data["summary"], str):
        return "summary must be a string"
    if not isinstance(data["checks"], list):
        return "checks must be a list"

    causes = {cause.value for cause in QAFailedCheckCause}
    for index, check in enumerate(data["checks"]):
        shape_error = _check_shape_error(index, check, causes)
        if shape_error:
            return shape_error

    # `pass` is false exactly when a product or access check failed. A check QA
    # had no tool for (`qa_capability`) is neither a failure nor a pass: the
    # runner records it as unverified and decides the verdict from the checks
    # that ran (`settle_unverified_checks`), so the executor's `pass` may say
    # either when those are its only failures. A not-applicable check is not a
    # failure; whether it stays one is decided by the runner's own evidence
    # (`_ground_not_applicable_checks`), not here.
    failed_causes = {check["cause"] for check in data["checks"] if check.get("pass") is False}
    judged_failure = bool(
        failed_causes
        - {
            QAFailedCheckCause.QA_CAPABILITY.value,
            QAFailedCheckCause.QA_TOOLING.value,
        }
    )
    if failed_causes and not judged_failure:
        return None
    if data["pass"] is judged_failure:
        return "pass must be false exactly when a product or qa_access check failed"
    return None
