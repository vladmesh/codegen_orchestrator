"""Git ref types shared by queue contracts."""

from typing import Annotated, Literal

from pydantic import StringConstraints

_SHA1_HEX_LENGTH = 40
_SHA256_HEX_LENGTH = 64

CommitSha = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=rf"^(?:[0-9a-fA-F]{{{_SHA1_HEX_LENGTH}}}|[0-9a-fA-F]{{{_SHA256_HEX_LENGTH}}})$",
    ),
]

# A commit-deploy message either carries a full commit SHA or nothing at all.
# Anything in between (a branch name, a short SHA) would let the deploy read a
# tree the caller never resolved.
OptionalCommitSha = CommitSha | Literal[""]
