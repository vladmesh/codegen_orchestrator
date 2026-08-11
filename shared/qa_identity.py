"""The account a QA run borrows on a deploy target, and how a target says it has one.

A central QA run reaches its target as somebody. That somebody is not the fleet:
the fleet key opens the administrative account (`servers.ssh_user`, `root` on a
row `server_sync` created), and a run holding root would be a run that can do
anything the platform can. So the run gets its own account — created by
provisioning, not minted at run time — and the runtime's only power over it is
to write one short-lived key into it and take that key back out.

One fact has to travel from provisioning to the QA runtime, and it lives in
`servers.labels`, next to `provisioning_phase`: whether this host was
provisioned by an Ansible that creates the QA account. The label is written by
the same code that writes `provisioning_phase=complete`, and that phase is the
one whose playbook creates the account — so "complete without a QA account" is
not a state the provisioner can produce.

What the label is asked, and what it is not:

* **whether it is there** is the question it answers. A host provisioned before
  the account existed carries no such label, and that is exactly the host the
  runtime has to tell apart from one where the account is waiting.
* **what is written in it** is not trusted. `servers.labels` is an untyped dict
  that `PATCH /api/servers/{handle}` will write, so treating its value as "the
  account to install a run key into" would let anything that can edit a server
  row point the runtime at an existing interactive account with sudo or docker —
  and the runtime would obediently mint access into it. That is the thing this
  module exists to prevent, so only the name provisioning itself writes
  (:data:`QA_SSH_USER`) is accepted as an identity; any other value is a refusal.

A consequence worth having: renaming the account later is fail-closed. Hosts
still carrying the old name lend nothing until the retrofit has run over them,
rather than lending an account nobody created.

The rejection reasons are not advice to retry. A target that cannot offer this
identity is refused exploratory QA, and the refusal is recorded against the
server as a provisioning fact, because that is what it is.
"""

from __future__ import annotations

from enum import StrEnum

from shared.contracts.dto.server import ServerDTO
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL

# `servers.labels` key holding the account a QA run borrows. Written by the
# infra-service provisioner, read by the QA runtime, and by nobody else.
QA_SSH_USER_LABEL = "qa_ssh_user"

# The account provisioning creates. The Ansible role that creates it defaults to
# the same name, and a unit test holds the two together.
QA_SSH_USER = "qa-observer"

# Accounts a run identity may never be, whatever a label says. `root` is the
# fleet's own reach into the machine; a QA run that borrowed it would be the
# platform testing itself with full authority over the thing under test.
PRIVILEGED_SSH_USERS = frozenset({"root"})


def provisioning_complete_labels() -> dict[str, str]:
    """The labels a finished software phase writes on the server row.

    One dict, written in one call, because the two facts are one fact: the phase
    that records itself complete is the phase whose playbook created the account.
    It lives here rather than in the provisioner so that the runtime's idea of a
    provisioned host and the provisioner's are the same object, and a test on
    either side of the boundary can hold the other to it.
    """
    return {
        PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE,
        QA_SSH_USER_LABEL: QA_SSH_USER,
    }


class QAIdentityRejection(StrEnum):
    """Why this server cannot lend a QA run an identity."""

    # No label at all: this host's provisioning predates the QA account, or
    # never finished. Either way nothing on it is waiting for a run key.
    NOT_PROVISIONED = "qa_identity_not_provisioned"
    # A label naming an account that is root, or the administrative account the
    # fleet key already opens. Neither is weaker than the fleet.
    PRIVILEGED = "qa_identity_privileged"
    # A label naming some other account. Whoever wrote it, provisioning did not:
    # provisioning writes one name. An account this platform did not create is an
    # account whose privileges nothing here knows, so it is not borrowed.
    NOT_ATTESTED = "qa_identity_not_attested"


def qa_identity_rejection(server: ServerDTO) -> QAIdentityRejection | None:
    """Return why this server cannot lend a QA identity, or ``None`` if it can."""
    recorded = (server.labels or {}).get(QA_SSH_USER_LABEL)
    if not recorded or not str(recorded).strip():
        return QAIdentityRejection.NOT_PROVISIONED
    account = str(recorded).strip()
    if account in PRIVILEGED_SSH_USERS or account == server.ssh_user:
        return QAIdentityRejection.PRIVILEGED
    # The label says a host was provisioned; it does not get to say by whom into
    # what. Only the account provisioning creates is one a run may borrow.
    if account != QA_SSH_USER:
        return QAIdentityRejection.NOT_ATTESTED
    return None


def qa_run_identity(server: ServerDTO) -> str:
    """The unprivileged account a QA run borrows on this server.

    Call it only after :func:`qa_identity_rejection` returned ``None``; a server
    that cannot lend an identity has none to return, and inventing one here is
    exactly the "mint access into somebody else's account" this module exists to
    prevent.
    """
    rejection = qa_identity_rejection(server)
    if rejection is not None:
        raise ValueError(f"{server.handle} lends no QA identity: {rejection.value}")
    return str(server.labels[QA_SSH_USER_LABEL]).strip()
