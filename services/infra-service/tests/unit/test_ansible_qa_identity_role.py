"""The account provisioning creates for QA runs, and the ceiling it lives under.

Three questions are asked here, and only the last two can be asked of a file:

* whether the role adopts an account it did not create. That is a decision, and
  it is written as one condition in the role, so the condition itself is what is
  evaluated below — with Jinja2, the same engine Ansible evaluates `that:` with,
  over the two worlds it exists to tell apart. What is tested is the role's own
  expression text, not a retyped version of it.
* what the role declares — that the account exists, that it is in no secondary
  group, that its sudo rule is one command, that it gets a read into the
  deployment tree and no write. These are read out of the YAML, because "not in
  the docker group" is a property of what the role says, and a role that stopped
  saying it would be the regression.
* what the target actually refuses, which is two real scripts run as scripts: the
  docker wrapper, and the proof the role puts the finished account through before
  anything records that this host has a QA identity. Both are executed here
  against stubs of the commands they consult, because both exist precisely for
  the case where the caller — or the role's own good intentions — are wrong.

Ansible is not installed in this workspace, so no test here claims to have run
the role against a host. What the two scripts prove is theirs alone — they are
executed — and what the YAML tests prove is what the role says; the fresh-host
acceptance against a real target is a separate card.
"""

from pathlib import Path
import re
import shutil
import subprocess

import jinja2
from jinja2 import meta
import pytest
import yaml

from shared.qa_identity import QA_SSH_USER
from shared.qa_target_profile import (
    QA_DOCKER_REQUIRED_VERBS,
    QA_TARGET_PROFILE_VERSION,
    proved_profile_version,
    qa_target_artefact_digest,
    wrapper_answer_problem,
)

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"
ROLE = ANSIBLE_DIR / "roles" / "qa_identity"
ROLE_TASKS = ROLE / "tasks" / "main.yml"
ROLE_DEFAULTS = ROLE / "defaults" / "main.yml"
WRAPPER = ROLE / "files" / "qa-docker"
PROOF = ROLE / "files" / "qa-identity-proof"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"
RETROFIT_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "qa_identity_retrofit.yml"

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _tasks() -> list[dict]:
    return yaml.safe_load(ROLE_TASKS.read_text())


def _task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


def _strings(value) -> list[str]:
    """Every scalar inside a task argument, each one a template on its own."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _defaults() -> dict:
    return yaml.safe_load(ROLE_DEFAULTS.read_text())


def _resolved_defaults() -> dict[str, str]:
    """The role's defaults with their references to each other filled in.

    `qa_ssh_group` is defined in terms of `qa_ssh_user`, so asking "what group
    does this role actually put the account in" means resolving one through the
    other rather than comparing template strings.
    """
    declared = {name: value for name, value in _defaults().items() if isinstance(value, str)}
    resolved: dict[str, str] = {}
    for _ in range(len(declared) + 1):
        for name, value in declared.items():
            filled = _VAR.sub(lambda m: resolved.get(m.group(1), m.group(0)), value)
            if "{{" not in filled:
                resolved[name] = filled
    return resolved


def _resolve(value: str) -> str:
    return _VAR.sub(lambda m: _resolved_defaults()[m.group(1)], value)


class TestTheAccountIsCreatedByProvisioning:
    def test_the_role_creates_the_account_the_runtime_looks_for(self):
        """The role's name for the account and the runtime's must be one name."""
        assert _defaults()["qa_ssh_user"] == QA_SSH_USER

        user = _task_named(_tasks(), "Create the QA observation account")["ansible.builtin.user"]
        assert user["name"] == "{{ qa_ssh_user }}"
        assert user["create_home"] is True

    def test_the_software_phase_is_what_creates_it(self):
        """`provisioning_phase=complete` is written when this playbook succeeds.

        So the role has to be inside it: that is what makes "complete, but with no
        account for QA to borrow" a state the provisioner cannot produce.
        """
        playbook = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())
        include = _task_named(playbook[0]["tasks"], "Create the QA run identity")
        assert include["ansible.builtin.include_role"]["name"] == "qa_identity"

    def test_the_retrofit_creates_the_same_account_from_the_same_role(self):
        """An old host and a fresh one get one identity, not two that look alike."""
        playbook = yaml.safe_load(RETROFIT_PLAYBOOK.read_text())
        include = _task_named(playbook[0]["tasks"], "Create the QA run identity")
        assert include["ansible.builtin.include_role"]["name"] == "qa_identity"

    def test_authorized_keys_is_opened_with_a_line_that_is_never_a_key(self):
        """The runtime appends and removes; it never creates this file.

        The sentinel is also what keeps the revoke's "an empty filter result is a
        failure" rule true for a file whose only other lines are run keys.
        """
        keys = _task_named(
            _tasks(), "Open the QA account's authorized_keys with a line that is never a key"
        )["ansible.builtin.lineinfile"]
        assert keys["path"] == "{{ qa_ssh_home }}/.ssh/authorized_keys"
        assert keys["owner"] == "{{ qa_ssh_user }}"
        assert keys["mode"] == "0600"
        assert keys["create"] is True
        assert _defaults()["qa_authorized_keys_sentinel"].strip().startswith("#")

    def test_an_identity_that_is_root_or_the_deploy_user_fails_provisioning(self):
        assertion = _task_named(
            _tasks(), "Refuse a QA identity that would not be its own unprivileged account"
        )["ansible.builtin.assert"]
        assert "qa_ssh_user != 'root'" in assertion["that"]
        assert "qa_ssh_user != deploy_user" in assertion["that"]


OWNERSHIP_GUARD = "Refuse an account of this name that this role did not create"
PROOF_TASK = "Prove on the target that this account is a QA seat that cannot become root"


def _task_index(name: str) -> int:
    return next(index for index, task in enumerate(_tasks()) if task["name"] == name)


def _guard_admits(*, account_exists: bool, marker_exists: bool) -> bool:
    """Evaluate the role's own refusal condition over one state of the target.

    The expressions are taken from the role and compiled with Jinja2 — the same
    engine Ansible evaluates `that:` items with — against the facts the two
    tasks above it register. Nothing about the condition is restated here, so a
    role that stopped asking the question cannot keep passing this.
    """
    conditions = _task_named(_tasks(), OWNERSHIP_GUARD)["ansible.builtin.assert"]["that"]
    context = {
        "qa_ssh_user": QA_SSH_USER,
        "ansible_facts": {
            "getent_passwd": {
                # `getent` with `fail_key: false` records a missing key as None
                # and a present one as its passwd fields.
                QA_SSH_USER: ["x", "1001", "1001", "", "/home/qa-observer", "/bin/bash"]
                if account_exists
                else None
            }
        },
        "qa_identity_owned": {"stat": {"exists": marker_exists}},
    }
    environment = jinja2.Environment(autoescape=False)  # noqa: S701 — shell/YAML, not HTML
    return all(environment.compile_expression(condition)(**context) for condition in conditions)


class TestAnAccountThisRoleDidNotCreateIsRefused:
    """A host may already carry `qa-observer`, and then nothing here knows what it is.

    The tasks that follow set a primary group, an exact supplementary list and a
    narrow sudoers file. None of them takes away `uid 0`, a rule in somebody
    else's file under `/etc/sudoers.d`, or an ACL on the docker socket — so an
    account somebody else created can survive all of them and still become root.
    Once the label is written the runtime writes a run key into that account, so
    the question "is this account ours" has to be answered before anything else,
    and answered on the machine.
    """

    def test_a_fresh_host_is_admitted(self):
        assert _guard_admits(account_exists=False, marker_exists=False) is True

    def test_a_host_this_role_already_ran_on_is_admitted(self):
        """The retrofit's whole job: come back to our own account and repair it."""
        assert _guard_admits(account_exists=True, marker_exists=True) is True

    def test_an_account_somebody_else_created_is_refused(self):
        """The collision case: `qa-observer` is there and this role never made it."""
        assert _guard_admits(account_exists=True, marker_exists=False) is False

    def test_a_run_interrupted_before_it_created_the_account_can_come_back(self):
        """The claim is laid first, so a half-finished run does not lock the host out."""
        assert _guard_admits(account_exists=False, marker_exists=True) is True

        assert _task_index("Record that this account is provisioning's own") < _task_index(
            "Create the QA observation account"
        )

    def test_the_claim_is_laid_only_after_the_refusal_could_have_happened(self):
        """Otherwise the role would own a stranger's account by writing a file."""
        assert _task_index(OWNERSHIP_GUARD) < _task_index(
            "Record that this account is provisioning's own"
        )

    def test_ownership_is_a_fact_on_the_target_and_not_a_row_in_the_database(self):
        """Root-owned, under /etc, unwritable by the account it speaks for."""
        marker = _task_named(_tasks(), "Record that this account is provisioning's own")[
            "ansible.builtin.copy"
        ]
        directory = _task_named(_tasks(), "Claim the account name this role is about to create")[
            "ansible.builtin.file"
        ]

        assert _resolve(marker["dest"]).startswith("/etc/")
        assert marker["owner"] == "root"
        assert marker["mode"] == "0644"
        assert directory["owner"] == "root"
        assert directory["mode"] == "0755"
        # One file per account name, so a rename cannot inherit the claim.
        assert QA_SSH_USER in _resolve(marker["dest"])

    def test_the_refusal_says_what_to_do_and_takes_nothing_away(self):
        """Somebody else's sudo policy is their data, not this role's mess to clean.

        Removing `/etc/sudoers.d/qa-observer-admin` to make the account fit would
        be deleting an administrator's decision, so the role stops instead — and
        a host that stops here keeps refusing QA, which is the truthful state.
        """
        guard = _task_named(_tasks(), OWNERSHIP_GUARD)["ansible.builtin.assert"]

        assert "was not created by this" in guard["fail_msg"]
        assert "{{ qa_identity_marker }}" in guard["fail_msg"]
        # Nothing in this role deletes anything on the target.
        assert "state: absent" not in ROLE_TASKS.read_text()


def _stub(directory: Path, name: str, body: str) -> None:
    script = directory / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)


SENTINEL = _defaults()["qa_authorized_keys_sentinel"].strip()

# `ssh-keygen` writes the throwaway key pair the login is attempted with, and
# `ssh` is the login itself. Both are stubbed by behaviour rather than by
# outcome: the client below admits exactly the key it finds in the file the
# proof wrote into, so a proof that stopped appending — or stopped putting the
# key where the account would read it — stops being admitted here too.
SSH_KEYGEN_STUB = """
out=""
comment=""
while [ $# -gt 0 ]; do
  case "$1" in
    -f) out=$2; shift ;;
    -C) comment=$2; shift ;;
  esac
  shift
done
[ -n "$out" ] || exit 2
echo "PRIVATE $comment" > "$out"
echo "ssh-ed25519 AAAAPROOFKEY $comment" > "$out.pub"
"""

SSH_STUB = """
key=""
while [ $# -gt 0 ]; do
  case "$1" in
    -i) key=$2; shift ;;
  esac
  shift
done
if [ "${STUB_SSHD:-admits}" != "admits" ]; then
  echo "Permission denied (publickey)." >&2
  exit 255
fi
if [ -z "$key" ] || [ ! -f "$key.pub" ]; then
  echo "no identity was offered" >&2
  exit 255
fi
if grep -q -F -- "$(cat "$key.pub")" "$STUB_KEYS" 2>/dev/null; then
  echo "${STUB_LOGIN_AS:-qa-observer}"
else
  echo "Permission denied (publickey)." >&2
  exit 255
fi
"""


def _seat(tmp_path: Path, *, keys: str | None = SENTINEL) -> Path:
    """The account home the target's passwd database will point the proof at.

    `keys=None` is the state run 33718999040 found: the account is there, its
    `.ssh` is there, and the file a QA run appends to is not.
    """
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    if keys is not None:
        (home / ".ssh" / "authorized_keys").write_text(keys + "\n")
    return home


SUDO_ONE_WRAPPER = """Matching Defaults entries for qa-observer on target:
    !requiretty

User qa-observer may run the following commands on target:
    (root) NOPASSWD: /usr/local/bin/qa-docker"""


class TestTheTargetProvesTheAccountCannotBecomeRoot:
    """The proof script, run as a script, against stubs of what it consults.

    Everything it asks is asked of the running system: `id`, sudo's own
    computation over every sudoers file, and a read attempted as the account
    itself. Each stub below is one of those answers, and each test is one shape
    of privileged account a pre-existing `qa-observer` can have — the cases the
    role's tasks pass straight over.
    """

    @pytest.fixture
    def socket(self, tmp_path) -> Path:
        path = tmp_path / "docker.sock"
        path.touch()
        return path

    def _target(  # noqa: PLR0913
        self,
        tmp_path,
        *,
        exists: bool = True,
        uid: str = "1001",
        groups: str = "qa-observer",
        sudo: str | None = SUDO_ONE_WRAPPER,
        socket_reachable: bool = False,
        keys: str | None = SENTINEL,
        wrapper_answer: str | None = None,
    ) -> Path:
        stubs = tmp_path / "bin"
        stubs.mkdir(exist_ok=True)
        home = _seat(tmp_path, keys=keys)
        _stub(
            stubs,
            "getent",
            "exit 2" if not exists else f'echo "{QA_SSH_USER}:x:1001:1001::{home}:/bin/bash"',
        )
        _stub(stubs, "ssh-keygen", SSH_KEYGEN_STUB)
        _stub(stubs, "ssh", SSH_STUB)
        _stub(
            stubs,
            "id",
            f'case "$1" in\n  -u) echo {uid} ;;\n  -nG) echo "{groups}" ;;\nesac',
        )
        _stub(
            stubs,
            "sudo",
            "exit 1" if sudo is None else f"cat <<'LISTING'\n{sudo}\nLISTING",
        )
        # `runuser -u ACCOUNT -- sudo -n WRAPPER version` is the account asking the
        # wrapper which profile it is; by default that is this role's own wrapper
        # file answering. Every other `runuser` is the socket question.
        answer = (
            f"exec {WRAPPER} version"
            if wrapper_answer is None
            else f"cat <<'ANSWER'\n{wrapper_answer}\nANSWER\nexit 0"
        )
        _stub(
            stubs,
            "runuser",
            f'if [ "$4" = "sudo" ]; then\n{answer}\nfi\nexit {0 if socket_reachable else 1}',
        )
        return stubs

    def _prove(
        self,
        stubs: Path,
        socket: Path,
        *,
        sshd: str = "admits",
        login_as: str = QA_SSH_USER,
    ) -> subprocess.CompletedProcess:
        keys = stubs.parent / "home" / ".ssh" / "authorized_keys"
        return subprocess.run(
            [
                str(PROOF),
                QA_SSH_USER,
                "/usr/local/bin/qa-docker",
                str(socket),
                SENTINEL,
                QA_TARGET_PROFILE_VERSION,
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{stubs}:/usr/bin:/bin",
                "STUB_KEYS": str(keys),
                "STUB_SSHD": sshd,
                "STUB_LOGIN_AS": login_as,
            },
        )

    def test_the_account_the_role_creates_passes(self, tmp_path, socket):
        result = self._prove(self._target(tmp_path), socket)

        assert result.returncode == 0, result.stderr
        assert "uid=1001" in result.stdout

    def test_uid_zero_is_refused(self, tmp_path, socket):
        """`qa-observer` with uid 0 is root wearing another name."""
        result = self._prove(self._target(tmp_path, uid="0"), socket)

        assert result.returncode != 0
        assert "uid 0" in result.stderr

    def test_a_pre_existing_account_in_the_docker_group_is_refused(self, tmp_path, socket):
        result = self._prove(
            self._target(tmp_path, groups="qa-observer docker"),
            socket,
        )

        assert result.returncode != 0
        assert "docker group" in result.stderr

    def test_somebody_elses_sudoers_rule_is_refused(self, tmp_path, socket):
        """The case the role's own file cannot see: a second rule, in a second file.

        The role writes `/etc/sudoers.d/qa-observer` and leaves every other file
        alone, so an account carrying `qa-observer ALL=(ALL) NOPASSWD: ALL` from
        somewhere else keeps it. Sudo is what knows; sudo is what is asked.
        """
        result = self._prove(
            self._target(
                tmp_path,
                sudo=f"{SUDO_ONE_WRAPPER}\n    (ALL) NOPASSWD: ALL",
            ),
            socket,
        )

        assert result.returncode != 0
        assert "may run more through sudo" in result.stderr

    def test_an_acl_straight_onto_the_docker_socket_is_refused(self, tmp_path, socket):
        """No group says so, and `id` cannot see it, so the account is asked instead."""
        result = self._prove(self._target(tmp_path, socket_reachable=True), socket)

        assert result.returncode != 0
        assert str(socket) in result.stderr

    def test_an_account_that_is_not_there_is_refused(self, tmp_path, socket):
        result = self._prove(self._target(tmp_path, exists=False), socket)

        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_sudo_that_cannot_answer_is_refused(self, tmp_path, socket):
        """An unprovable seat is a failed seat: silence is never taken for absence."""
        result = self._prove(self._target(tmp_path, sudo=None), socket)

        assert result.returncode != 0

    def test_the_role_runs_this_proof_and_does_it_last(self):
        """It is a proof about the finished account, so it comes after the work."""
        proof = _task_named(_tasks(), PROOF_TASK)["ansible.builtin.script"]

        assert "qa-identity-proof" in proof["cmd"]
        assert "{{ qa_ssh_user | quote }}" in proof["cmd"]
        assert "{{ qa_docker_wrapper | quote }}" in proof["cmd"]
        assert "{{ qa_docker_socket | quote }}" in proof["cmd"]
        # The line the role opens `authorized_keys` with travels into the proof,
        # so "this file is the one this role wrote" is asked with the role's own
        # sentinel rather than with a copy of it kept in the script.
        assert "{{ qa_authorized_keys_sentinel | quote }}" in proof["cmd"]
        # The profile the wrapper must answer with travels in from the role's
        # defaults, the same value the orchestrator pins.
        assert "{{ qa_target_profile_version | quote }}" in proof["cmd"]
        assert _task_index(PROOF_TASK) == len(_tasks()) - 1

    def test_the_proof_reports_the_profile_it_proved(self, tmp_path, socket):
        """The receipt is written from this line, so it has to name the exact profile."""
        result = self._prove(self._target(tmp_path), socket)

        assert result.returncode == 0, result.stderr
        assert proved_profile_version(result.stdout) == QA_TARGET_PROFILE_VERSION

    @pytest.mark.parametrize(
        "answer",
        [
            # The wrapper production target 5wwb carried: it predates `version`.
            "qa-docker: docker version is refused on this host; "
            "allowed: diff inspect logs port ps stats top",
            "qa-docker profile=0123456789abcdef verbs=diff inspect logs port ps read-contract "
            "stats top version",
        ],
    )
    def test_a_seat_whose_wrapper_is_not_the_current_profile_is_refused(
        self, tmp_path, socket, answer
    ):
        """Same path, same sudo rule, older file: the seat is not the one QA speaks to."""
        result = self._prove(self._target(tmp_path, wrapper_answer=answer), socket)

        assert result.returncode != 0
        assert f"not QA target profile {QA_TARGET_PROFILE_VERSION}" in result.stderr
        assert "qa_target_version=" not in result.stdout

    # A host whose own name does not resolve makes sudo print this on stderr
    # before it runs anything, and the account asks the wrapper through sudo.
    SUDO_WARNING = "sudo: unable to resolve host vps-275301: Name or service not known"

    @pytest.mark.parametrize("where", ["before", "after"])
    def test_a_sudo_warning_beside_the_answer_is_not_an_old_wrapper(self, tmp_path, socket, where):
        """What vps-275301's retrofit was refused on, with the right wrapper installed.

        `sudo` writes that warning to stderr, the proof reads the answer with
        stderr joined on — deliberately, because a wrapper that fails has
        nothing else to say — and a host that cannot resolve its own name was
        then reported as one carrying a wrapper of some other profile. The
        wrapper's own line is what answers the question, so it is the line that
        is read.
        """
        current = (
            f"qa-docker profile={QA_TARGET_PROFILE_VERSION} "
            f"verbs={' '.join(sorted(QA_DOCKER_REQUIRED_VERBS))}"
        )
        noisy = (
            f"{self.SUDO_WARNING}\n{current}"
            if where == "before"
            else f"{current}\n{self.SUDO_WARNING}"
        )

        result = self._prove(self._target(tmp_path, wrapper_answer=noisy), socket)

        assert result.returncode == 0, result.stderr
        assert proved_profile_version(result.stdout) == QA_TARGET_PROFILE_VERSION

    def test_a_sudo_warning_does_not_excuse_a_wrapper_of_another_profile(self, tmp_path, socket):
        """Tolerating the noise is not tolerating the answer underneath it."""
        noisy = (
            f"{self.SUDO_WARNING}\nqa-docker profile=0123456789abcdef "
            f"verbs={' '.join(sorted(QA_DOCKER_REQUIRED_VERBS))}"
        )

        result = self._prove(self._target(tmp_path, wrapper_answer=noisy), socket)

        assert result.returncode != 0
        assert f"not QA target profile {QA_TARGET_PROFILE_VERSION}" in result.stderr

    def test_a_failed_proof_is_what_stops_the_identity_being_recorded(self):
        """The provisioner writes the label only when the playbook succeeded.

        So a proof that fails fails the role, fails the playbook, and leaves the
        host recorded as one with no QA identity — which is how "the account
        might be root" reaches an administrator as a provisioning failure rather
        than as a QA run that quietly could not find an account.
        """
        for playbook in (SOFTWARE_PLAYBOOK, RETROFIT_PLAYBOOK):
            tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
            include = _task_named(tasks, "Create the QA run identity")
            assert include["ansible.builtin.include_role"]["name"] == "qa_identity"
            # Nothing in either playbook goes on regardless of the role failing.
            assert "ignore_errors" not in include
            assert "failed_when" not in include


class TestTheTargetProvesAQARunCanTakeTheSeat:
    """The other half of the proof, and the half run 33718999040 needed.

    That host was recorded `complete` with the QA-account label, and its
    `qa-observer` had no `authorized_keys` at all: every check that existed then
    was about what the account may not do, and none of them about whether
    anybody could sit in it. So the proof ends by doing what the runtime does —
    append one key, log in with it, take it back out — against a stubbed
    `ssh-keygen` and a stubbed client that admits exactly the key it finds in
    the file the proof wrote into.
    """

    @pytest.fixture
    def socket(self, tmp_path) -> Path:
        path = tmp_path / "docker.sock"
        path.touch()
        return path

    _target = TestTheTargetProvesTheAccountCannotBecomeRoot._target
    _prove = TestTheTargetProvesTheAccountCannotBecomeRoot._prove

    def _keys(self, tmp_path) -> Path:
        return tmp_path / "home" / ".ssh" / "authorized_keys"

    def test_a_seat_that_can_be_taken_passes_and_keeps_no_key_of_the_proof(self, tmp_path, socket):
        result = self._prove(self._target(tmp_path), socket)

        assert result.returncode == 0, result.stderr
        assert "login=ok" in result.stdout
        # The file is handed back exactly as provisioning left it: the proof
        # writes into a live `authorized_keys`, and a key of its own left behind
        # would be a standing login nobody issued.
        assert self._keys(tmp_path).read_text() == SENTINEL + "\n"

    def test_an_account_with_no_authorized_keys_is_refused(self, tmp_path, socket):
        """The state the fifth paid run reached, made a provisioning failure."""
        result = self._prove(self._target(tmp_path, keys=None), socket)

        assert result.returncode != 0
        assert "nothing to write its key into" in result.stderr

    def test_an_authorized_keys_this_role_did_not_open_is_refused(self, tmp_path, socket):
        """A file of that name whose provenance is unknown proves nothing.

        The runtime removes a run key by filtering this file and refuses to
        write back an empty result, which only holds while the line the role
        opened it with is still in it.
        """
        result = self._prove(
            self._target(tmp_path, keys="ssh-ed25519 AAAASOMEBODYELSE somebody@else"),
            socket,
        )

        assert result.returncode != 0
        assert "does not carry the line this role opens it with" in result.stderr

    def test_an_account_sshd_will_not_admit_is_refused_and_the_key_still_comes_out(
        self, tmp_path, socket
    ):
        """A locked account, a group-writable home, an sshd that names its users.

        None of them are visible in the file, so the file is not what is asked.
        The key still has to come back out of a host that failed: the proof must
        not leave a login behind on a target it is about to fail provisioning on.
        """
        result = self._prove(self._target(tmp_path), socket, sshd="refuses")

        assert result.returncode != 0
        assert "sshd refused a key written into" in result.stderr
        assert "Permission denied" in result.stderr
        assert self._keys(tmp_path).read_text() == SENTINEL + "\n"

    def test_a_login_that_lands_on_another_account_is_refused(self, tmp_path, socket):
        """The seat has to be this account, not merely some account on the host."""
        result = self._prove(self._target(tmp_path), socket, login_as="root")

        assert result.returncode != 0
        assert "instead" in result.stderr

    def test_the_role_puts_the_ssh_client_the_proof_needs_on_the_target(self):
        """The proof takes the seat, so the target needs a client to take it with.

        A fresh provider image is not promised to carry one, and the retrofit
        runs this role on hosts nobody chose the image of.
        """
        names = {task["name"] for task in _tasks()}
        install = next(
            task
            for task in _tasks()
            if task.get("ansible.builtin.apt", {}).get("name") == "openssh-client"
        )

        assert install["ansible.builtin.apt"]["state"] == "present"
        assert _task_index(install["name"]) < _task_index(PROOF_TASK)
        assert PROOF_TASK in names


def _apply_user_module(before: dict, params: dict) -> dict:
    """What `ansible.builtin.user` does to one account's group membership.

    Ansible is not installed in this workspace, so the role cannot be executed
    against a target from here; what can be done without hand-waving is to take
    the parameters the role really declares — read out of its YAML, not retyped —
    and put them through the module's documented contract:

        `group` sets the primary group. `groups` is the *supplementary* list, and
        `append` decides whether that list is added to the account's existing
        supplementary groups or replaces them exactly.
        https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/user_module.html

    The two memberships are kept apart here for the same reason the role has to
    keep them apart: conflating them is the defect this models the absence of. A
    role that omits `group:` leaves the primary group untouched, and this model
    says so — see the test that runs it against exactly that.
    """
    primary = params["group"] if "group" in params else before["primary"]
    if "groups" in params:
        declared = set(params["groups"])
        supplementary = (
            set(before["supplementary"]) | declared if params.get("append") else declared
        )
    else:
        supplementary = set(before["supplementary"])
    return {"primary": primary, "supplementary": supplementary}


def _groups_of(account: dict) -> set[str]:
    return {account["primary"], *account["supplementary"]}


def _reaches_the_docker_socket(account: dict) -> bool:
    """`/var/run/docker.sock` is `root:docker`, mode 0660.

    So the whole question is group membership, and the account's primary group
    counts for exactly as much as a supplementary one does.
    """
    return "docker" in _groups_of(account)


class TestTheAccountCannotBecomeRoot:
    def test_it_is_in_no_secondary_group_at_all(self):
        """Membership of `docker` is root on the host, so there is no group list."""
        user = _task_named(_tasks(), "Create the QA observation account")["ansible.builtin.user"]
        assert user["groups"] == []
        # An exact supplementary list is what takes back groups added by hand.
        assert user["append"] is False

    def test_its_primary_group_is_its_own_and_the_role_creates_it(self):
        """Named explicitly, because the primary group is not `groups`'s business."""
        tasks = _tasks()
        user = _task_named(tasks, "Create the QA observation account")["ansible.builtin.user"]
        group = _task_named(tasks, "Create the QA account's own group")["ansible.builtin.group"]

        assert _resolve(user["group"]) == QA_SSH_USER
        assert _resolve(group["name"]) == _resolve(user["group"])
        assert group["state"] == "present"

    def test_a_primary_group_that_would_be_root_on_the_host_fails_provisioning(self):
        assertion = _task_named(
            _tasks(), "Refuse a QA identity that would not be its own unprivileged account"
        )["ansible.builtin.assert"]

        assert "qa_ssh_group not in ['docker', 'root', 'sudo']" in assertion["that"]


class TestARetrofitTakesAnExistingAccountOutOfDocker:
    """The half of the card that is about hosts which already exist.

    A retrofit runs the same role over an account that may already be there, and
    may already be there wrong — created by hand inside `docker`, which is where
    somebody would put it if they wanted it to be able to run containers. What
    the role declares has to end that, and "ends it" is a statement about the
    account afterwards, not about the role's intentions.
    """

    @pytest.fixture
    def user_params(self) -> dict:
        params = _task_named(_tasks(), "Create the QA observation account")["ansible.builtin.user"]
        return {
            key: (_resolve(value) if isinstance(value, str) else value)
            for key, value in params.items()
        }

    @pytest.fixture
    def already_in_docker(self) -> dict:
        """`qa-observer` as a host may already hold it: primary group `docker`."""
        return {"primary": "docker", "supplementary": {"docker", "sudo"}}

    def test_the_account_comes_out_of_docker_by_both_memberships(
        self, user_params, already_in_docker
    ):
        after = _apply_user_module(already_in_docker, user_params)

        assert after["primary"] == QA_SSH_USER
        assert after["supplementary"] == set()
        assert "docker" not in _groups_of(after)
        assert not _reaches_the_docker_socket(after)

    def test_running_it_again_over_the_repaired_account_changes_nothing(
        self, user_params, already_in_docker
    ):
        once = _apply_user_module(already_in_docker, user_params)
        twice = _apply_user_module(once, user_params)

        assert twice == once
        assert not _reaches_the_docker_socket(twice)

    def test_without_a_primary_group_the_repair_would_not_happen(
        self, user_params, already_in_docker
    ):
        """The assertion above has teeth: this is the role as it was, and it fails.

        `groups: []` with `append: false` empties the supplementary list and says
        nothing about the primary one, so an account created inside `docker`
        stays inside `docker` and keeps the socket.
        """
        without_primary = {key: value for key, value in user_params.items() if key != "group"}

        after = _apply_user_module(already_in_docker, without_primary)

        assert after["primary"] == "docker"
        assert _reaches_the_docker_socket(after)

    def test_no_task_in_the_role_grants_docker_group_or_socket_access(self):
        role_text = ROLE_TASKS.read_text()
        assert "docker.sock" not in role_text
        assert "groups: docker" not in role_text

    def test_sudo_is_one_command_and_that_command_is_the_wrapper(self):
        sudoers = _task_named(_tasks(), "Allow the QA account that one command and nothing else")[
            "ansible.builtin.copy"
        ]
        rules = [
            line
            for line in sudoers["content"].splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        assert "{{ qa_ssh_user }} ALL=(root) NOPASSWD: {{ qa_docker_wrapper }}" in rules
        # One command spec, and it is the wrapper. Anything wider than this is
        # the account being able to become root.
        assert [rule for rule in rules if not rule.startswith("Defaults:")] == [
            "{{ qa_ssh_user }} ALL=(root) NOPASSWD: {{ qa_docker_wrapper }}"
        ]
        # A broken sudoers file is a host nobody can administer, so it is checked
        # before it is installed.
        assert sudoers["validate"] == "visudo -cf %s"
        assert sudoers["mode"] == "0440"
        assert sudoers["owner"] == "root"

    def test_the_wrapper_belongs_to_root(self):
        """An account that can rewrite the wrapper is an account with root."""
        wrapper = _task_named(_tasks(), "Install the read-only docker wrapper")[
            "ansible.builtin.copy"
        ]
        assert wrapper["owner"] == "root"
        assert wrapper["group"] == "root"
        assert wrapper["mode"] == "0755"

    def test_the_deployment_tree_is_readable_and_not_writable(self):
        """A read is granted by name, not by joining the group that can write."""
        acl = _task_named(
            _tasks(), "Let the QA account into the deployment tree without giving it a write"
        )["ansible.posix.acl"]
        assert acl["path"] == "{{ services_root }}"
        assert acl["entity"] == "{{ qa_ssh_user }}"
        assert acl["permissions"] == "rx"
        assert "w" not in acl["permissions"]


class TestTheRetrofitRemovesOnlyWhatItCanIdentify:
    """Cleanup runs in the administrative account's home, so it may not guess.

    That home is a person's home too. Every path the retrofit deletes has to be
    one the removed `qa_runner` role itself created, at a name nothing else uses;
    anything that is also ordinary interactive data stays where it is and is
    named in the host's report, which is worth more than a deletion nobody can
    undo.
    """

    @staticmethod
    def _playbook_tasks() -> list[dict]:
        return yaml.safe_load(RETROFIT_PLAYBOOK.read_text())[0]["tasks"]

    @staticmethod
    def _removed() -> list[str]:
        return _task_named(
            TestTheRetrofitRemovesOnlyWhatItCanIdentify._playbook_tasks(),
            "Remove what the target-local QA agent left behind",
        )["loop"]

    @staticmethod
    def _left() -> list[dict]:
        """What stayed, why it stayed, and the command that removes it by hand."""
        return _task_named(
            TestTheRetrofitRemovesOnlyWhatItCanIdentify._playbook_tasks(),
            "Look at what is deliberately left in place",
        )["loop"]

    @staticmethod
    def _left_in_place() -> list[str]:
        return [item["path"] for item in TestTheRetrofitRemovesOnlyWhatItCanIdentify._left()]

    def test_it_removes_exactly_the_old_runner_s_own_artefacts(self):
        home = "{{ qa_residue_home }}"
        assert self._removed() == [
            f"{home}/.local/bin/claude",
            f"{home}/.claude/.credentials.json",
            f"{home}/.qa-telethon.env",
            "/opt/qa-runner",
        ]

    def test_the_claude_directory_itself_survives(self):
        """One credentials file was the platform's; the directory around it is not.

        `~/.claude` on an administrative account also holds whatever that account
        does with Claude Code interactively — settings, history, backups — and
        deleting it recursively is the user-data loss this playbook promises not
        to cause.
        """
        home = "{{ qa_residue_home }}"
        assert f"{home}/.claude" not in self._removed()
        assert f"{home}/.claude/.credentials.json" in self._removed()
        assert f"{home}/.claude" in self._left_in_place()

    def test_swap_is_left_alone_and_said_so(self):
        """`/swapfile` cannot be told from any other swap file on the host.

        The old role made 2GB of swap there, and so does every guide an
        administrator follows. Taking swap away from a live host running user
        applications is an outage, not cleanup, so it stays — and the host says
        it stayed, with the reason and with the command that removes it, rather
        than the playbook quietly deciding either way.

        The earlier version of this test asserted that the string `swapoff`
        appeared nowhere in the playbook. It does appear now, inside the report
        text this test reads: naming the command an administrator would run is
        the point, and the assertion below says the thing that actually matters —
        no task acts on swap.
        """
        [swap] = [item for item in self._left() if item["path"] == "/swapfile"]

        assert "/swapfile" not in self._removed()
        assert "outage" in swap["why"]
        assert swap["remove_by_hand"].startswith("swapoff /swapfile")
        # And no task does it: the only mention of swap is in what is reported.
        for task in self._playbook_tasks():
            module = next(
                key
                for key in task
                if key not in ("name", "loop", "loop_control", "register", "when", "become")
            )
            if module == "ansible.builtin.debug":
                continue
            assert "swap" not in yaml.safe_dump(task[module])

    def test_what_stays_is_reported_with_a_reason_and_a_way_to_remove_it(self):
        """Left in place is a decision handed on, not a decision avoided."""
        for item in self._left():
            assert item["why"].strip()
            assert item["path"] in item["remove_by_hand"]

    def test_every_host_reports_both_what_went_and_what_stayed(self):
        """A fleet-wide run has to be readable per host, including its refusals."""
        report = _task_named(
            self._playbook_tasks(), "Report what this host changed and what it left"
        )["ansible.builtin.debug"]["msg"]

        assert "qa_residue_removed.results" in report["removed_paths"]
        assert "selectattr('changed')" in report["removed_paths"]
        # The whole item travels into the report, so each surviving path arrives
        # with its reason and its removal command beside it.
        assert "qa_residue_left.results" in report["left_in_place"]
        assert "selectattr('stat.exists')" in report["left_in_place"]
        assert "map(attribute='item')" in report["left_in_place"]
        # And what the target said about the account, not what the role intended,
        # under the key the provisioner cuts this line out of the play output by
        # — the same one `provision_software.yml` reports under, because one
        # marker has to find whichever play created the seat.
        assert "qa_identity_proof.stdout" in report["qa_identity_proof"]


class TestAPlayReportReadsOnlyWhatThePlayHas:
    """A play-level task is templated in the play's scope, never in a role's.

    `include_role` is dynamic: the defaults of the role it pulls in live inside
    the include and are gone again by the time a later play task is templated,
    unless the include says `public`. The retrofit's own host report named
    `qa_ssh_user` — a default of `qa_identity` — and both managed production
    targets failed on it after every change the playbook makes had already been
    applied, which is the worst place for a play to stop: the work is done and
    the caller is told the host is broken.

    So every variable a play-level report names is checked here against what the
    play can actually see: its own vars and vars_files, the facts its tasks and
    its roles' tasks register, and the defaults of the roles it includes
    publicly. Registered variables are host facts and survive any include; role
    defaults do not.
    """

    # Supplied by Ansible itself or by the caller's `-e`, not by this play.
    AMBIENT = frozenset({"inventory_hostname", "ansible_facts", "item", "target_host"})

    def _play(self, playbook: Path) -> dict:
        return yaml.safe_load(playbook.read_text())[0]

    def _role(self, name: str) -> Path:
        return ANSIBLE_DIR / "roles" / name

    @staticmethod
    def _facts(task: dict) -> set[str]:
        """What one task leaves behind as a host fact, which outlives any scope."""
        names = {task["register"]} if "register" in task else set()
        for key in ("set_fact", "ansible.builtin.set_fact"):
            names |= {name for name in (task.get(key) or {}) if name != "cacheable"}
        return names

    def _visible(self, playbook: Path) -> set[str]:
        play = self._play(playbook)
        names = set(self.AMBIENT) | set(play.get("vars") or {})
        for relative in play.get("vars_files") or []:
            names |= set(yaml.safe_load((playbook.parent / relative).read_text()) or {})
        for task in [
            *(play.get("pre_tasks") or []),
            *play["tasks"],
            *(play.get("post_tasks") or []),
        ]:
            names |= self._facts(task)
            include = task.get("ansible.builtin.include_role") or task.get("include_role")
            if include is None:
                continue
            role_tasks = yaml.safe_load(
                (self._role(include["name"]) / "tasks" / "main.yml").read_text()
            )
            for role_task in role_tasks or []:
                names |= self._facts(role_task)
            if not include.get("public"):
                continue
            for scope in ("defaults", "vars"):
                source = self._role(include["name"]) / scope / "main.yml"
                if source.exists():
                    names |= set(yaml.safe_load(source.read_text()) or {})
        return names

    def _reports(self, playbook: Path):
        for task in self._play(playbook)["tasks"]:
            body = task.get("ansible.builtin.debug", task.get("debug"))
            if body is not None:
                yield task["name"], body

    @pytest.mark.parametrize(
        "playbook",
        [SOFTWARE_PLAYBOOK, RETROFIT_PLAYBOOK],
        ids=lambda path: path.name,
    )
    def test_every_variable_a_play_level_report_names_is_in_scope(self, playbook):
        visible = self._visible(playbook)

        env = jinja2.Environment(autoescape=False)  # noqa: S701

        for name, body in self._reports(playbook):
            referenced: set[str] = set()
            for text in _strings(body):
                referenced |= meta.find_undeclared_variables(env.parse(text))
            missing = sorted(referenced - visible)

            assert not missing, f"{playbook.name}: {name!r} reads {missing}, which the play has not"

    def test_the_retrofit_report_reads_the_role_defaults_through_a_public_include(self):
        """The fix, stated where a later edit would have to undo it deliberately."""
        include = _task_named(self._play(RETROFIT_PLAYBOOK)["tasks"], "Create the QA run identity")

        assert include["ansible.builtin.include_role"]["public"] is True


class TestTheTargetRefusesWhatWrites:
    """The wrapper, run as the target runs it, against a docker that records."""

    @pytest.fixture
    def docker(self, tmp_path):
        """A stand-in docker on PATH that records the argv it was reached with."""
        log = tmp_path / "docker.log"
        stub = tmp_path / "docker"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n')
        stub.chmod(0o755)
        return log

    def _wrapper(self, docker_log: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(WRAPPER), *args],
            capture_output=True,
            text=True,
            env={"PATH": f"{docker_log.parent}:/usr/bin:/bin"},
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ["exec", "weather-bot-backend-1", "sh"],
            ["run", "-v", "/:/host", "alpine", "sh"],
            ["cp", "/etc/shadow", "weather-bot-backend-1:/tmp/x"],
            ["compose", "restart"],
            ["build", "-t", "x", "."],
            ["commit", "weather-bot-backend-1"],
            ["save", "alpine"],
            ["rm", "-f", "weather-bot-backend-1"],
            ["network", "create", "escape"],
            ["--host", "tcp://127.0.0.1:2375", "logs", "weather-bot-backend-1"],
        ],
    )
    def test_a_sub_command_that_writes_or_escapes_never_reaches_docker(self, docker, argv):
        result = self._wrapper(docker, *argv)

        assert result.returncode != 0
        assert "refused" in result.stderr
        assert not docker.exists(), f"docker was reached with {argv}"

    def test_a_call_with_no_sub_command_is_refused(self, docker):
        result = self._wrapper(docker)

        assert result.returncode != 0
        assert not docker.exists()

    @pytest.mark.parametrize(
        "argv",
        [
            ["read-contract", "backend", "/etc/passwd", "262144"],
            ["read-contract", "backend", "infra/.env", "262144"],
            ["read-contract", "backend", "codegen_kit/_active_packages.py", "1"],
            ["read-contract", "backend", "codegen_kit/_active_packages.py", "262144", "sh"],
        ],
    )
    def test_a_contract_read_with_any_unbounded_argument_is_refused(self, docker, argv):
        result = self._wrapper(docker, *argv)

        assert result.returncode != 0
        assert not docker.exists()

    def test_the_fixed_contract_read_reaches_only_the_named_container_and_path(self, docker):
        result = self._wrapper(
            docker,
            "read-contract",
            "weather-bot-backend-1",
            "codegen_kit/_active_packages.py",
            "262144",
        )

        assert result.returncode == 0, result.stderr
        reached = docker.read_text()
        assert reached.startswith("exec weather-bot-backend-1 sh -c ")
        assert reached.endswith(" _ codegen_kit/_active_packages.py 262144\n")

    @pytest.mark.parametrize(
        "argv",
        [
            ["logs", "--tail", "200", "weather-bot-backend-1"],
            ["inspect", "--format", "{{json .State}}", "weather-bot-backend-1"],
            ["diff", "weather-bot-backend-1"],
            ["port", "weather-bot-backend-1"],
            ["top", "weather-bot-backend-1"],
            ["stats", "--no-stream", "weather-bot-backend-1"],
            # The run's capability set is resolved with this one, so it has to
            # pass here; which containers the answer may contain is decided in
            # the orchestrator, not on the host.
            ["ps", "--all", "--filter", "label=com.docker.compose.project=weather-bot"],
        ],
    )
    def test_a_read_reaches_docker_unchanged(self, docker, argv):
        result = self._wrapper(docker, *argv)

        assert result.returncode == 0, result.stderr
        assert docker.read_text().strip() == " ".join(argv)

    def test_the_wrapper_allows_exactly_the_reads_the_runtime_can_ask_for(self):
        """The runtime's container-scoped set must be a subset of the target's."""
        allowed = next(
            line for line in WRAPPER.read_text().splitlines() if line.startswith("ALLOWED=")
        )
        names = set(allowed.split('"')[1].split())
        assert {"diff", "inspect", "logs", "port", "stats", "top"} <= names
        assert "ps" in names, "capability resolution asks docker which containers this project has"
        assert QA_DOCKER_REQUIRED_VERBS <= names

    def test_version_answers_the_pinned_profile_and_every_required_verb(self, docker):
        result = self._wrapper(docker, "version")

        assert result.returncode == 0, result.stderr
        assert wrapper_answer_problem(result.stdout) is None
        assert not docker.exists(), "version is answered by the wrapper, not by docker"

    def test_version_takes_no_arguments_and_never_reaches_docker(self, docker):
        result = self._wrapper(docker, "version", "--format", "{{json .}}")

        assert result.returncode != 0
        assert not docker.exists()


class TestTheProfileVersionIsBoundToTheRoleFiles:
    """`QA_TARGET_PROFILE_VERSION` is what these files say, mechanically.

    A change to any role artefact — the wrapper's verbs above all — changes the
    derived version, so this fails until the pinned constant, the wrapper's
    line and the defaults' line are updated together. A host that still carries
    the old files then answers an old profile and is refused.
    """

    def test_the_pinned_version_is_derived_from_the_role_files(self):
        assert qa_target_artefact_digest(ROLE) == QA_TARGET_PROFILE_VERSION

    def test_the_wrapper_and_the_defaults_carry_the_pinned_version(self):
        assert f"QA_TARGET_PROFILE_VERSION={QA_TARGET_PROFILE_VERSION}\n" in WRAPPER.read_text()
        assert _defaults()["qa_target_profile_version"] == QA_TARGET_PROFILE_VERSION

    def test_changing_a_role_artefact_changes_the_version(self, tmp_path):
        copy = tmp_path / "qa_identity"
        shutil.copytree(ROLE, copy)
        wrapper = copy / "files" / "qa-docker"
        wrapper.write_text(wrapper.read_text().replace(" read-contract", "", 1))

        assert qa_target_artefact_digest(copy) != QA_TARGET_PROFILE_VERSION

    def test_the_version_lines_alone_do_not_change_the_version(self, tmp_path):
        """Otherwise the version could never be written into the files it describes."""
        copy = tmp_path / "qa_identity"
        shutil.copytree(ROLE, copy)
        wrapper = copy / "files" / "qa-docker"
        wrapper.write_text(
            wrapper.read_text().replace(QA_TARGET_PROFILE_VERSION, "ffffffffffffffff")
        )

        assert qa_target_artefact_digest(copy) == QA_TARGET_PROFILE_VERSION
