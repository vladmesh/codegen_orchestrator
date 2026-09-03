"""What became of one confirmed `initial_settings` value at deploy time.

The Product Brief says which typed values the product starts life with
(`shared/contracts/dto/product_brief.py::InitialSetting`); the generated
product's own `settings.set` write path is the only way they get there. This
module is the third thing: the bounded, credential-safe record of what that
write path answered, per setting, stored on the deploy run beside every other
outcome of that run.

Bounded and credential-safe means exactly what it says. An outcome names the
setting the way the product identifies it — key, scope, subject — and one
closed-set reason. It never carries the deployment capability, the value that
was written, a response body, or an exception string, because a deploy run
result is read back by the scheduler, by QA, and by people.

Every failure makes the deploy `DeployOutcome.SETTINGS_SEED_FAILED`:
`DeployRunResult` refuses to call a run successful while it records any seed
failure, so no producer and no reconciliation can turn a setting that never
arrived into a deploy handed to QA.

`SETTINGS_SEED_CONVERGENT_FAILURES` answers a narrower routing question. A
failure in that set may answer differently after a same-commit redeploy, so a
mixed result retries if it contains even one such failure. An all-deterministic
result — an undeclared key, schema-refused value, or missing write capability —
goes straight to the artifact-repair terminal path instead of consuming the
deploy retry bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from shared.contracts.dto.product_brief import SettingScope


class SettingsSeedFailureKind(StrEnum):
    """The closed set of reasons one confirmed setting was not proved written."""

    #: The deployed product's environment contract does not declare
    #: `SETTINGS_WRITE_CAPABILITY` — an existing pinned product, generated
    #: before the settings core. The confirmed value cannot be written.
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    #: The product could not be reached at all.
    TRANSPORT = "transport"
    #: `POST /settings/set` answered 404: the key is not declared in the
    #: product's own `services/<service>/manifest.yaml` `settings_schema`.
    KEY_NOT_DECLARED = "key_not_declared"
    #: `POST /settings/set` answered 422: the confirmed value does not satisfy
    #: the schema the product declared for that key.
    VALUE_REJECTED = "value_rejected"
    #: `POST /settings/set` refused for some other reason.
    SET_REJECTED = "set_rejected"
    #: The write was accepted and `POST /settings/get` refused to answer.
    READBACK_REJECTED = "readback_rejected"
    #: The readback answered something that is not this setting's value.
    MALFORMED_READBACK = "malformed_readback"
    #: The readback answered, and it disagrees with what was written.
    READBACK_MISMATCH = "readback_mismatch"


#: The exact Core settings v1 response detail for an undeclared manifest key.
#: It distinguishes the documented endpoint response from a generic route 404.
CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL = "Setting key not declared"

#: The exact Core settings v1 response detail for a manifest-schema rejection.
CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL = "Setting value does not satisfy its declared schema"


#: The failures a second deploy of the same commit may answer differently.
#: This controls supervisor routing only: every failure still blocks SUCCESS.
SETTINGS_SEED_CONVERGENT_FAILURES = frozenset(
    {
        SettingsSeedFailureKind.TRANSPORT,
        SettingsSeedFailureKind.SET_REJECTED,
        SettingsSeedFailureKind.READBACK_REJECTED,
        SettingsSeedFailureKind.MALFORMED_READBACK,
        SettingsSeedFailureKind.READBACK_MISMATCH,
    }
)


class SettingSeedOutcome(BaseModel):
    """One confirmed setting, and whether the product proved it holds it.

    `written` is true only when the value was set *and* read back equal — the
    same "prove it, do not assume it" shape the generated grant proof uses.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    scope: SettingScope
    subject_id: int | None = None
    written: bool
    failure: SettingsSeedFailureKind | None = None

    @model_validator(mode="after")
    def _written_and_failure_are_one_answer(self) -> SettingSeedOutcome:
        if self.written == (self.failure is not None):
            raise ValueError(
                "a seeded setting is either proved written or carries exactly one failure kind"
            )
        return self


def settings_seed_failure_kinds(
    outcomes: Sequence[SettingSeedOutcome],
) -> tuple[SettingsSeedFailureKind, ...]:
    """Return every failure kind once, in stable diagnostic order."""
    return tuple(sorted({outcome.failure for outcome in outcomes if outcome.failure is not None}))


def settings_seed_failure_detail(failures: Sequence[SettingsSeedFailureKind]) -> str:
    """Render an already-canonical failure set for bounded diagnostics."""
    return ",".join(failure.value for failure in failures)
