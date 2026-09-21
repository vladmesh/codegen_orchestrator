"""The QA target receipt a managed server carries, and the wrapper answer it is held to.

The binding of :data:`QA_TARGET_PROFILE_VERSION` to the role files is tested next
to those files, in `services/infra-service/tests/unit/test_ansible_qa_identity_role.py`.
"""

from datetime import UTC, datetime

import pytest

from shared.contracts.dto.server import ServerDTO
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL
from shared.qa_target_profile import (
    QA_DOCKER_REQUIRED_VERBS,
    QA_TARGET_PROFILE_VERSION,
    QATargetReceiptRejection,
    proved_profile_version,
    qa_target_receipt_rejection,
    wrapper_answer_problem,
)
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL

_NOW = datetime.now(UTC)
_VERBS = " ".join(sorted(QA_DOCKER_REQUIRED_VERBS))


def _server(**overrides) -> ServerDTO:
    base = {
        "handle": "vps-1",
        "host": "203.0.113.1",
        "public_ip": "203.0.113.1",
        "ssh_user": "root",
        "status": "ready",
        "is_managed": True,
        "labels": {
            PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE,
            QA_SSH_USER_LABEL: QA_SSH_USER,
        },
        "qa_target_version": QA_TARGET_PROFILE_VERSION,
        "qa_target_proved_at": _NOW,
        "created_at": _NOW,
    }
    base.update(overrides)
    return ServerDTO(**base)


def test_a_current_receipt_proves_the_target():
    assert qa_target_receipt_rejection(_server()) is None


def test_labels_alone_are_not_a_receipt():
    """`qa_ssh_user` and `provisioning_phase=complete` say nothing about the artefacts."""
    server = _server(qa_target_version=None, qa_target_proved_at=None)

    assert qa_target_receipt_rejection(server) is QATargetReceiptRejection.MISSING


def test_a_version_without_a_proof_time_is_not_a_receipt():
    server = _server(qa_target_proved_at=None)

    assert qa_target_receipt_rejection(server) is QATargetReceiptRejection.MISSING


def test_a_receipt_for_another_profile_is_stale():
    server = _server(qa_target_version="0" * len(QA_TARGET_PROFILE_VERSION))

    assert qa_target_receipt_rejection(server) is QATargetReceiptRejection.STALE


def test_the_current_wrapper_answer_is_accepted():
    answer = f"qa-docker profile={QA_TARGET_PROFILE_VERSION} verbs={_VERBS}\n"

    assert wrapper_answer_problem(answer) is None


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_sudo_warning_beside_the_answer_is_not_an_old_wrapper(where):
    """A target that cannot resolve its own name still answers for its wrapper.

    `sudo` prints `unable to resolve host ...` around the command it runs, and
    the retrofit of vps-275301 was refused for it while the current wrapper was
    installed. The wrapper's own line is what names the profile.
    """
    warning = "sudo: unable to resolve host vps-275301: Name or service not known"
    current = f"qa-docker profile={QA_TARGET_PROFILE_VERSION} verbs={_VERBS}"
    answer = f"{warning}\n{current}" if where == "before" else f"{current}\n{warning}"

    assert wrapper_answer_problem(answer) is None


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        # The wrapper production target 5wwb carried: no `version` verb at all.
        (
            "qa-docker: docker version is refused on this host; "
            "allowed: diff inspect logs port ps stats top",
            "no profile answer",
        ),
        ("", "no profile answer"),
        (f"qa-docker profile=0123456789abcdef verbs={_VERBS}", "0123456789abcdef"),
        (
            f"qa-docker profile={QA_TARGET_PROFILE_VERSION} verbs=diff inspect logs port ps "
            "stats top version",
            "read-contract",
        ),
    ],
)
def test_an_old_or_incomplete_wrapper_is_named(answer, names):
    problem = wrapper_answer_problem(answer)

    assert problem is not None
    assert names in problem


def test_the_proved_version_is_read_from_the_play_output():
    output = (
        '"qa_identity_proof": "qa-identity-proof: qa-observer uid=1001 login=ok '
        f'qa_target_version={QA_TARGET_PROFILE_VERSION}"'
    )

    assert proved_profile_version(output) == QA_TARGET_PROFILE_VERSION
    assert proved_profile_version("PLAY RECAP ok=12") is None
