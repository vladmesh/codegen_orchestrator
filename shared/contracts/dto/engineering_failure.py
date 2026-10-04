"""Worker-safe canonical engineering failure vocabulary."""

from enum import StrEnum


class EngineeringFailureReason(StrEnum):
    """Stable classifications for an engineering run that produced nothing usable."""

    # The worker reported success, but its commit adds no file change over the
    # story branch head the attempt started from (`pre_attempt_head_sha` on the
    # attempt): it is that head, a commit behind it — the branch base, an
    # already-deployed commit, an earlier task's commit — or commits that net out
    # to nothing. Nothing was produced, so the run is failed with its own name
    # instead of being accepted as a success nobody did.
    WORKER_COMMIT_NOT_PUBLISHED = "worker_commit_not_published"
    NO_NEW_COMMIT = "no_new_commit"
    # The commit's environment contract declares a required production `derived`
    # key the platform cannot compute, so any deploy of it would fail in the
    # secret resolver. The keys travel in `uncomputable_derived_keys`.
    UNCOMPUTABLE_DERIVED_KEY = "uncomputable_derived_key"
