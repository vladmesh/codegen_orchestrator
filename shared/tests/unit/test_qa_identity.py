"""Which account a QA run may borrow, decided from the server row alone.

The rule has to be readable from one place by both sides of the boundary: the
provisioner writes the label, the QA runtime reads it, and neither may invent an
account when the other did not put one there.
"""

from datetime import UTC, datetime

import pytest

from shared.contracts.dto.server import ServerDTO
from shared.qa_identity import (
    QA_SSH_USER,
    QA_SSH_USER_LABEL,
    QAIdentityRejection,
    provisioning_complete_labels,
    qa_identity_rejection,
    qa_run_identity,
)
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL


def _server(*, ssh_user: str = "root", labels: dict | None = None) -> ServerDTO:
    return ServerDTO(
        handle="vps-1",
        host="vps-1.example.com",
        public_ip="1.2.3.4",
        ssh_user=ssh_user,
        status="active",
        is_managed=True,
        labels=labels if labels is not None else {},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_a_provisioned_host_lends_the_account_provisioning_created():
    server = _server(labels=provisioning_complete_labels())

    assert qa_identity_rejection(server) is None
    assert qa_run_identity(server) == QA_SSH_USER


def test_the_completion_labels_say_both_things_at_once():
    """A phase that is complete is a phase that created the account."""
    assert provisioning_complete_labels() == {
        PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE,
        QA_SSH_USER_LABEL: QA_SSH_USER,
    }


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({}, QAIdentityRejection.NOT_PROVISIONED),
        # A host whose software phase finished before the account existed.
        (
            {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
            QAIdentityRejection.NOT_PROVISIONED,
        ),
        ({QA_SSH_USER_LABEL: ""}, QAIdentityRejection.NOT_PROVISIONED),
        ({QA_SSH_USER_LABEL: "   "}, QAIdentityRejection.NOT_PROVISIONED),
        ({QA_SSH_USER_LABEL: "root"}, QAIdentityRejection.PRIVILEGED),
        # Anything provisioning did not write. `servers.labels` is an untyped
        # dict the server API will PATCH, so a value here is a claim, not a fact,
        # and an account this platform never created is one whose privileges it
        # knows nothing about.
        ({QA_SSH_USER_LABEL: "deploy"}, QAIdentityRejection.NOT_ATTESTED),
        ({QA_SSH_USER_LABEL: "ubuntu"}, QAIdentityRejection.NOT_ATTESTED),
        ({QA_SSH_USER_LABEL: f"{QA_SSH_USER}-2"}, QAIdentityRejection.NOT_ATTESTED),
    ],
)
def test_a_host_that_lends_nothing_says_why(labels, expected):
    assert qa_identity_rejection(_server(labels=labels)) is expected


def test_a_label_pointing_at_an_account_provisioning_did_not_create_lends_nothing():
    """The label answers "was this host provisioned", not "as whom".

    Editing a server row must not be a way to choose whose `authorized_keys` the
    runtime writes into. `deploy` exists on plenty of hosts, is in the `docker`
    group there, and is a perfectly ordinary thing to put in an untyped dict —
    so the value is checked against the one name provisioning writes, and
    nothing else is an identity.
    """
    server = _server(ssh_user="root", labels={QA_SSH_USER_LABEL: "deploy"})

    assert qa_identity_rejection(server) is QAIdentityRejection.NOT_ATTESTED
    with pytest.raises(ValueError, match="lends no QA identity"):
        qa_run_identity(server)


def test_renaming_the_account_leaves_hosts_carrying_the_old_name_refused():
    """Fail-closed, which is what makes the retrofit the way to rename it."""
    server = _server(labels={QA_SSH_USER_LABEL: "qa-observer-old"})

    assert qa_identity_rejection(server) is QAIdentityRejection.NOT_ATTESTED


def test_the_administrative_account_is_not_an_identity_a_run_may_borrow():
    """Whatever the label says, the account the fleet key opens is not weaker."""
    server = _server(ssh_user="deploy", labels={QA_SSH_USER_LABEL: "deploy"})

    assert qa_identity_rejection(server) is QAIdentityRejection.PRIVILEGED


def test_asking_for_an_identity_a_host_does_not_lend_raises():
    """There is no value to return, and returning `ssh_user` would be the bug."""
    with pytest.raises(ValueError, match="lends no QA identity"):
        qa_run_identity(_server())
