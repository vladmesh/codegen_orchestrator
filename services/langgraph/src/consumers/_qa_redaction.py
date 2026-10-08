"""The one set of secrets a central QA run must never let out.

A run's runtime handles several kinds of secret, and each must stay out of what
the executor receives and out of everything the run keeps:

* the product's generated capabilities, read from the project's own encrypted
  secrets on the management host — the caller-identity capability `http_get`
  presents, the users-grant capability the preflight presents, the jobs-fire
  capability `fire_job` presents, the settings-write capability the stand
  language write presents. A product can reflect one back in a body, a
  log line or an error;
* the QA Telegram credentials the sandbox is handed once this run proved them,
  which an agent with a shell can print;
* the capability endpoint's run token, which the executor holds and can print.

They live in one object per run. Each secret is added where it enters the run —
the stored capabilities where the consumer reads them (`consumers/qa.py`), the
Telegram credentials where the runtime enters the run (`run_qa_centrally`), the
token where the endpoint mints it (`QACapabilityService`), and any value a call
presents where the calls are built (`build_qa_callables`) — and every scrub
reads that same object. Nothing passes a separate tuple of secrets around, so no
composition can drop one kind.

It is applied at two boundaries:

* the executor boundary — every call result, before the executor receives it
  (`build_qa_callables` wraps every call; `_dispatch` scrubs probe input);
* the retention boundary — every sink that is kept or leaves the process: the
  workspace's trace, observations, probe and Telegram evidence, report and
  verdict, the runner's transcript, and the run's result.

Scrubbing happens before any bounding, so a cut never leaves a fragment that an
exact match would miss; a text that was already cut by someone else (a target's
`head -c`, the probe CLI) has a trailing prefix of a value redacted as well.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

#: The product's generated capabilities a QA run may hold, by env-contract name.
USER_IDENTITY_CAPABILITY = "USER_IDENTITY_CAPABILITY"
USERS_GRANT_CAPABILITY = "USERS_GRANT_CAPABILITY"
JOBS_FIRE_CAPABILITY = "JOBS_FIRE_CAPABILITY"  # noqa: S105 — a name, not a value
SETTINGS_WRITE_CAPABILITY = "SETTINGS_WRITE_CAPABILITY"
RUN_CAPABILITIES = (
    USER_IDENTITY_CAPABILITY,
    USERS_GRANT_CAPABILITY,
    JOBS_FIRE_CAPABILITY,
    SETTINGS_WRITE_CAPABILITY,
)

#: What replaces a value, by kind, so a reader of the evidence knows what was there.
REDACTED = "[redacted: QA run capability]"
TELEGRAM_CREDENTIAL = "[redacted: QA Telegram credential]"
ENDPOINT_TOKEN = "[redacted: QA capability endpoint token]"  # noqa: S105 — a label
#: A shorter trailing prefix matches too much text to be a value's fragment.
_MIN_FRAGMENT = 8


class QARunRedaction:
    """Every secret of one QA run, and the one way any text is scrubbed of them.

    It only grows: a secret is added where it enters the run, and every holder
    of this object sees it from then on. Its `repr` names no value.
    """

    def __init__(self, secrets: Iterable[str] = (), *, label: str = REDACTED) -> None:
        self._labels: dict[str, str] = {}
        self.add(*secrets, label=label)

    @classmethod
    def from_stored(cls, stored: Mapping[str, object]) -> QARunRedaction:
        """A run's set, starting with every capability the project's secrets hold."""
        return cls(
            value
            for name in RUN_CAPABILITIES
            if isinstance(value := stored.get(name), str) and value
        )

    def add(self, *values: str | None, label: str = REDACTED) -> None:
        """Add the secrets that just entered the run; an empty value adds nothing."""
        for value in values:
            if value and value not in self._labels:
                self._labels[value] = label

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._labels)

    def __repr__(self) -> str:
        return f"QARunRedaction({len(self._labels)} secrets)"

    def text(self, text: str) -> str:
        """`text` with every secret, and a trailing fragment of one, replaced."""
        if not text:
            return text
        # Longest first, so a secret containing another is replaced whole.
        ordered = sorted(self._labels.items(), key=lambda item: -len(item[0]))
        for secret, label in ordered:
            text = text.replace(secret, label)
        for secret, label in ordered:
            for length in range(min(len(secret) - 1, len(text)), _MIN_FRAGMENT - 1, -1):
                if text.endswith(secret[:length]):
                    text = text[:-length] + label
                    break
        return text

    def value(self, value: Any) -> Any:
        """Any call result or evidence object, with every string in it scrubbed."""
        if not self._labels:
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
