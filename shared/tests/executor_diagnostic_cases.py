"""Synthetic host-session profile observations for executor-diagnostic fixtures.

A v2 host-session diagnostic carries the profile its reason code is derived
from. Fixtures that only need "both executors ready" or "both unusable" take the
matching credential-free observation from here instead of restating it.
"""

from shared.contracts.dto.executor_diagnostics import (
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    ProfileLoginState,
    RefreshMaterialState,
)

_OBSERVATIONS = {
    ExecutorProfileCondition.HEALTHY: (ProfileLoginState.LOGGED_IN, RefreshMaterialState.PRESENT),
    ExecutorProfileCondition.LOGGED_OUT: (
        ProfileLoginState.LOGGED_OUT,
        RefreshMaterialState.MISSING,
    ),
    ExecutorProfileCondition.REFRESH_MISSING: (
        ProfileLoginState.LOGGED_IN,
        RefreshMaterialState.MISSING,
    ),
    ExecutorProfileCondition.UNUSABLE: (ProfileLoginState.UNKNOWN, RefreshMaterialState.UNKNOWN),
    ExecutorProfileCondition.UNVERIFIABLE: (
        ProfileLoginState.UNKNOWN,
        RefreshMaterialState.PRESENT,
    ),
    ExecutorProfileCondition.READ_CONTENDED: (
        ProfileLoginState.UNKNOWN,
        RefreshMaterialState.UNKNOWN,
    ),
}

_CONDITION_FOR_REASON = {
    "ready": ExecutorProfileCondition.HEALTHY,
    "inventory_unreconciled": ExecutorProfileCondition.HEALTHY,
    "local_auth_invalid": ExecutorProfileCondition.UNUSABLE,
    "profile_logged_out": ExecutorProfileCondition.LOGGED_OUT,
    "profile_refresh_missing": ExecutorProfileCondition.REFRESH_MISSING,
    "profile_metadata_unverifiable": ExecutorProfileCondition.UNVERIFIABLE,
    "profile_read_contended": ExecutorProfileCondition.READ_CONTENDED,
}


def host_profile(condition: ExecutorProfileCondition) -> ExecutorProfileObservation:
    """A credential-free observation for a condition that needs no expiry instant."""
    login_state, refresh_material = _OBSERVATIONS[condition]
    return ExecutorProfileObservation(
        condition=condition, login_state=login_state, refresh_material=refresh_material
    )


def host_profile_for_reason(reason_code: str) -> ExecutorProfileObservation:
    """The observation a host-session diagnostic with this reason code carries."""
    return host_profile(_CONDITION_FOR_REASON[reason_code])
