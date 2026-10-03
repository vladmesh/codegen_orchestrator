"""The one set of capability values a central QA run must never let out.

A run holds up to three of the product's generated capabilities, all read from
the project's own encrypted secrets on the management host: the caller-identity
capability its `http_get` presents, the users-grant capability its preflight
presents, and the jobs-fire capability its `fire_job` presents. Each leaves the
runtime only as a request header. A product can still reflect one back — in a
response body, a log line, an error — and from there it would reach the
executor or the run's retained evidence.

So the values are held in one runtime-only set, built once where the run
resolves its secrets (`consumers/qa.py`), and applied at two boundaries:

* the executor boundary — every call result, before the executor receives it
  (`build_qa_callables` wraps every call);
* the retention boundary — every sink that is kept or leaves the process: the
  workspace's trace, observations, probe and Telegram evidence, report and
  verdict, the runner's transcript redaction, and the run's result.

Scrubbing happens before any bounding, so a cut never leaves a fragment that an
exact match would miss; a text that was already cut by someone else (a target's
`head -c`, the probe CLI) has a trailing prefix of a value redacted as well.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

#: The product's generated capabilities a QA run may hold, by env-contract name.
USER_IDENTITY_CAPABILITY = "USER_IDENTITY_CAPABILITY"
USERS_GRANT_CAPABILITY = "USERS_GRANT_CAPABILITY"
JOBS_FIRE_CAPABILITY = "JOBS_FIRE_CAPABILITY"  # noqa: S105 — a name, not a value
RUN_CAPABILITIES = (USER_IDENTITY_CAPABILITY, USERS_GRANT_CAPABILITY, JOBS_FIRE_CAPABILITY)

REDACTED = "[redacted: QA run capability]"
#: A shorter trailing prefix matches too much text to be a value's fragment.
_MIN_FRAGMENT = 8


@dataclass(frozen=True)
class QARunRedaction:
    """The capability values of one run. Never printed: `repr` omits them."""

    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_stored(cls, stored: Mapping[str, object]) -> QARunRedaction:
        """Every run capability the project's decrypted secrets hold."""
        return cls(
            tuple(
                value
                for name in RUN_CAPABILITIES
                if isinstance(value := stored.get(name), str) and value
            )
        )

    def including(self, *values: str | None) -> QARunRedaction:
        """This set, plus any value a caller holds directly."""
        merged = dict.fromkeys(self.secrets)
        merged.update(dict.fromkeys(value for value in values if value))
        return QARunRedaction(tuple(merged))

    def text(self, text: str) -> str:
        """`text` with every value, and a trailing fragment of one, replaced."""
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        for secret in self.secrets:
            for length in range(min(len(secret) - 1, len(text)), _MIN_FRAGMENT - 1, -1):
                if text.endswith(secret[:length]):
                    text = text[:-length] + REDACTED
                    break
        return text

    def value(self, value: Any) -> Any:
        """Any call result or evidence object, with every string in it scrubbed."""
        if not self.secrets:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, BaseModel):
            return type(value).model_validate(self.value(value.model_dump(mode="python")))
        if isinstance(value, dict):
            return {self.value(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        return value
