"""Passive, credential-free reading helpers for host-session profile readers.

The helpers turn locally stored metadata into typed instants. They never return,
log or raise with a token, a claim payload or a filesystem path.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import math
import re

from shared.contracts.dto.executor_diagnostics import (
    CredentialExpirySource,
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    LastRefreshSource,
    ProfileLoginState,
    RefreshMaterialState,
)

_JWT_SEGMENTS = 3
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9_-]*$")
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_LATEST = datetime(2200, 1, 1, tzinfo=UTC)
_FRACTION = re.compile(r"(\.\d{6})\d+")

#: Profile files are a few kilobytes; the bound keeps every diagnostics tick bounded.
MAX_JSON_BYTES = 1 << 20
#: Pinned serde_json 1.0.149 starts `remaining_depth` at 128 and fails when a
#: container would take it to 0, so the deepest accepted nesting is 127.
MAX_JSON_NESTING = 127
_U64_MAX = 2**64 - 1
_I32_MAX = 2**31 - 1
_I32_MIN = -(2**31)
#: serde_json's `POW10: [f64; 309]`, the correctly rounded literals 1e0..1e308.
_POW10 = tuple(float(f"1e{power}") for power in range(309))
_NUMBER_TOKEN = re.compile(r"-?(\d+)(?:\.(\d+))?(?:[eE]([+-]?)(\d+))?")


class _JsonParseFailure:
    """The one failure result of `load_json`; never equal to a parsed JSON value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "JSON_PARSE_FAILURE"


JSON_PARSE_FAILURE = _JsonParseFailure()


class _JsonRejected(ValueError):
    """A JSON document the boundary refuses. Carries no document content."""


def load_json(raw: str | bytes, *, pinned_serde_json: bool) -> object:
    """The single total JSON trust boundary for every host-session profile reader.

    Order: bound the input size; decode strict UTF-8 (a BOM is not skipped);
    refuse nesting deeper than `MAX_JSON_NESTING`; parse. With
    `pinned_serde_json` the parse also refuses duplicate object keys,
    NaN/Infinity constants, every numeric token pinned serde_json 1.0.149
    (no `float_roundtrip`) reports as `NumberOutOfRange`, and lone surrogates.
    Without it (Claude), Python JSON semantics apply inside the same bounds.
    Returns the parsed value or `JSON_PARSE_FAILURE`; it never raises.
    """
    try:
        if isinstance(raw, bytes):
            if len(raw) > MAX_JSON_BYTES:
                return JSON_PARSE_FAILURE
            text = raw.decode("utf-8")
        else:
            if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
                return JSON_PARSE_FAILURE
            text = raw
        if _nesting_exceeds(text, MAX_JSON_NESTING):
            return JSON_PARSE_FAILURE
        hooks = (
            {
                "object_pairs_hook": _unique_json_object,
                "parse_constant": _reject_json_constant,
                "parse_int": _serde_json_integer,
                "parse_float": _serde_json_float,
            }
            if pinned_serde_json
            else {}
        )
        value = json.loads(text, **hooks)
        if pinned_serde_json and _contains_lone_surrogate(value):
            return JSON_PARSE_FAILURE
    except (ValueError, RecursionError, TypeError, OverflowError):
        return JSON_PARSE_FAILURE
    return value


def _nesting_exceeds(text: str, limit: int) -> bool:
    """Count container nesting outside strings without recursing."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    if len({key for key, _value in pairs}) != len(pairs):
        raise _JsonRejected("duplicate JSON object key")
    return dict(pairs)


def _reject_json_constant(_name: str) -> object:
    raise _JsonRejected("serde_json does not accept NaN or Infinity")


def _serde_json_integer(token: str) -> int:
    if serde_json_number_out_of_range(token):
        raise _JsonRejected("NumberOutOfRange")
    return int(token)


def _serde_json_float(token: str) -> float:
    if serde_json_number_out_of_range(token):
        raise _JsonRejected("NumberOutOfRange")
    return float(token)


def _contains_lone_surrogate(value: object) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
            strings: list = list(item.keys())
        elif isinstance(item, list):
            pending.extend(item)
            continue
        elif isinstance(item, str):
            strings = [item]
        else:
            continue
        for string in strings:
            try:
                string.encode("utf-8")
            except UnicodeEncodeError:
                return True
    return False


def _u64_overflows(significand: int, digit: int) -> bool:
    """serde_json's `overflow!(significand * 10 + digit, u64::MAX)`."""
    return significand >= _U64_MAX // 10 and (significand > _U64_MAX // 10 or digit > _U64_MAX % 10)


def serde_json_number_out_of_range(token: str) -> bool:
    """Whether pinned serde_json 1.0.149 fails a value parse of this token.

    Mirrors `parse_integer`, `parse_long_integer`, `parse_decimal`,
    `parse_decimal_overflow`, `parse_exponent`, `parse_exponent_overflow` and
    the non-`float_roundtrip` `f64_from_parts`: an integer that fits `u64`
    (or `i64` when negative) is exact; otherwise at most `u64` significand
    digits are kept, later integer digits raise the exponent, later fraction
    digits are dropped, the exponent saturates in `i32`, and the result is
    out of range exactly when that f64 computation reaches infinity.
    """
    match = _NUMBER_TOKEN.fullmatch(token)
    if match is None:
        return True
    integer, fraction, exponent_sign, exponent_digits = match.groups()
    significand = int(integer[0])
    exponent = 0
    long_integer = False
    for position, char in enumerate(integer[1:], start=1):
        digit = int(char)
        if _u64_overflows(significand, digit):
            # parse_long_integer counts this digit and every later one.
            exponent = len(integer) - position
            long_integer = True
            break
        significand = significand * 10 + digit
    if fraction is None and exponent_digits is None and not long_integer:
        return False
    if fraction is not None:
        for char in fraction:
            digit = int(char)
            if _u64_overflows(significand, digit):
                break  # parse_decimal_overflow ignores every further fraction digit
            significand = significand * 10 + digit
            exponent -= 1
    if exponent_digits is not None:
        exp = int(exponent_digits[0])
        for char in exponent_digits[1:]:
            digit = int(char)
            if exp >= _I32_MAX // 10 and (exp > _I32_MAX // 10 or digit > _I32_MAX % 10):
                # parse_exponent_overflow: an error instead of infinity, else zero.
                return significand != 0 and exponent_sign != "-"
            exp = exp * 10 + digit
        exponent = exponent - exp if exponent_sign == "-" else exponent + exp
        exponent = max(_I32_MIN, min(_I32_MAX, exponent))
    return _f64_from_parts_is_infinite(significand, exponent)


def _f64_from_parts_is_infinite(significand: int, exponent: int) -> bool:
    value = float(significand)
    while True:
        magnitude = abs(exponent)
        if magnitude < len(_POW10):
            return exponent >= 0 and math.isinf(value * _POW10[magnitude])
        if value == 0.0:
            return False
        if exponent >= 0:
            return True
        value /= 1e308
        exponent += 308


#: A last-refresh stamp further ahead than this contradicts the local clock.
LAST_REFRESH_CLOCK_SKEW = timedelta(minutes=5)


class MetadataError(ValueError):
    """Stored time metadata is malformed or contradictory. Carries no detail."""


@dataclass(frozen=True)
class ProfileInspection:
    """One passive reading: the safe observation and a fixed refusal message.

    `refusal` is set exactly when the profile holds no usable refresh-capable
    material, so worker creation and diagnostics refuse the same profiles. A
    contended or unverifiable read proves neither way and carries no refusal.
    """

    observation: ExecutorProfileObservation
    refusal: str | None

    def require_refresh_capable(self) -> None:
        if self.refusal is not None:
            raise RuntimeError(self.refusal)


def unusable(refusal: str) -> ProfileInspection:
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.UNUSABLE,
            login_state=ProfileLoginState.UNKNOWN,
            refresh_material=RefreshMaterialState.UNKNOWN,
        ),
        refusal,
    )


def read_contended() -> ProfileInspection:
    """A CLI was rewriting the profile and no stable read completed in the bound."""
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.READ_CONTENDED,
            login_state=ProfileLoginState.UNKNOWN,
            refresh_material=RefreshMaterialState.UNKNOWN,
        ),
        None,
    )


def logged_out(refusal: str) -> ProfileInspection:
    return ProfileInspection(
        ExecutorProfileObservation(
            condition=ExecutorProfileCondition.LOGGED_OUT,
            login_state=ProfileLoginState.LOGGED_OUT,
            refresh_material=RefreshMaterialState.MISSING,
        ),
        refusal,
    )


@dataclass(frozen=True)
class ProfileFacts:
    """Parsed, credential-free time facts and whether any stored value was bad."""

    session_expires_at: datetime | None = None
    session_expiry_source: CredentialExpirySource | None = None
    refresh_expires_at: datetime | None = None
    refresh_expiry_source: CredentialExpirySource | None = None
    last_refresh_at: datetime | None = None
    last_refresh_source: LastRefreshSource | None = None
    metadata_invalid: bool = False

    def observation(
        self,
        condition: ExecutorProfileCondition,
        login_state: ProfileLoginState,
        refresh_material: RefreshMaterialState,
    ) -> ExecutorProfileObservation:
        return ExecutorProfileObservation(
            condition=condition,
            login_state=login_state,
            refresh_material=refresh_material,
            session_expires_at=self.session_expires_at,
            session_expiry_source=self.session_expiry_source,
            refresh_expires_at=self.refresh_expires_at,
            refresh_expiry_source=self.refresh_expiry_source,
            last_refresh_at=self.last_refresh_at,
            last_refresh_source=self.last_refresh_source,
        )

    def refresh_missing(self, now: datetime, refusal: str) -> ProfileInspection:
        """Session material without a refresh credential cannot be renewed."""
        if self.metadata_invalid:
            login_state = ProfileLoginState.UNKNOWN
        elif self.session_expires_at is not None and self.session_expires_at <= now:
            login_state = ProfileLoginState.EXPIRED
        else:
            login_state = ProfileLoginState.LOGGED_IN
        return ProfileInspection(
            self.observation(
                ExecutorProfileCondition.REFRESH_MISSING,
                login_state,
                RefreshMaterialState.MISSING,
            ),
            refusal,
        )


def epoch_instant(value: object, *, milliseconds: bool = False) -> datetime:
    """A timezone-aware UTC instant from a stored numeric epoch."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MetadataError
    if not math.isfinite(value):
        raise MetadataError
    seconds = value / 1000 if milliseconds else value
    try:
        instant = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise MetadataError from exc
    if not _EARLIEST <= instant < _LATEST:
        raise MetadataError
    return instant


def iso_instant(value: object) -> datetime:
    """A timezone-aware UTC instant from a stored ISO-8601 string; naive is invalid."""
    if not isinstance(value, str):
        raise MetadataError
    try:
        # Rust writers keep nanoseconds; Python keeps microseconds.
        instant = datetime.fromisoformat(_FRACTION.sub(r"\1", value))
    except ValueError as exc:
        raise MetadataError from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise MetadataError
    instant = instant.astimezone(UTC)
    if not _EARLIEST <= instant < _LATEST:
        raise MetadataError
    return instant


def _json_segment(segment: str) -> object | None:
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        return None
    value = load_json(decoded, pinned_serde_json=True)
    return None if value is JSON_PARSE_FAILURE else value


def jwt_expiry(token: str) -> datetime | None:
    """Return the `exp` of a structurally valid JWT, never its payload.

    A value is a JWT only when it has three base64url segments whose header is a
    JSON object naming `alg` and whose payload is a JSON object. Anything else is
    an opaque token and has no locally provable expiry (`None`). A JWT whose time
    claims are malformed or contradictory raises `MetadataError`. The signature
    is not verified: this is local metadata, not an authentication decision.
    """
    segments = token.split(".")
    if (
        len(segments) != _JWT_SEGMENTS
        or not _BASE64URL.match(segments[0])
        or not _BASE64URL.match(segments[1])
        or not _SIGNATURE.match(segments[2])
    ):
        return None
    header = _json_segment(segments[0])
    payload = _json_segment(segments[1])
    if (
        not isinstance(header, dict)
        or not isinstance(header.get("alg"), str)
        or not isinstance(payload, dict)
    ):
        return None
    if payload.get("exp") is None:
        return None
    expires_at = epoch_instant(payload["exp"])
    if payload.get("iat") is not None and epoch_instant(payload["iat"]) > expires_at:
        raise MetadataError
    return expires_at
