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

The split that matters is `SETTINGS_SEED_RETRYABLE_FAILURES`. A failure in that
set says the product did not answer the way a working product answers, and the
same commit deployed again may well seed cleanly — so it holds the deploy back
as `DeployOutcome.SETTINGS_SEED_FAILED` and goes round under the bound that
already stops a failing deploy from looping. That outcome is deliberately its
own: `DeployRunResult` refuses to call a run successful while it records a
failure from this set, so no producer and no reconciliation can turn a setting
that never arrived into a deploy handed to QA.

Every other failure is deterministic in this commit: an undeclared key
stays undeclared and a schema-refused value stays refused however often the
same artifact is redeployed, so retrying is a loop that cannot converge. Those
are reported alongside the successful deploy instead, where the run's readers
see that a confirmed setting did not reach the product and that the repair is a
new plan, not another deploy.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from shared.contracts.dto.product_brief import SettingScope


class SettingsSeedFailureKind(StrEnum):
    """The closed set of reasons one confirmed setting was not proved written."""

    #: The deployed product's environment contract does not declare
    #: `SETTINGS_WRITE_CAPABILITY` — an existing pinned product, generated
    #: before the settings core. Nothing was attempted and nothing failed.
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


#: The failures that hold the deploy back. See the module docstring: these are
#: the ones a second deploy of the same commit can answer differently.
SETTINGS_SEED_RETRYABLE_FAILURES = frozenset(
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
