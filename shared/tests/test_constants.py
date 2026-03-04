"""Tests for shared constants."""

from shared.constants import CI, Paths, Provisioning, Timeouts


class TestPaths:
    def test_ssh_key_default(self):
        assert Paths.SSH_KEY == "/root/.ssh/id_ed25519"

    def test_ansible_playbooks_default(self):
        assert Paths.ANSIBLE_PLAYBOOKS == "/app/ansible/playbooks"

    def test_playbook_helper(self):
        result = Paths.playbook("setup.yml")
        assert result == f"{Paths.ANSIBLE_PLAYBOOKS}/setup.yml"


class TestTimeouts:
    def test_ssh_command(self):
        assert Timeouts.SSH_COMMAND == 30

    def test_provisioning(self):
        assert Timeouts.PROVISIONING == 1200

    def test_reinstall(self):
        assert Timeouts.REINSTALL == 900

    def test_password_reset(self):
        assert Timeouts.PASSWORD_RESET == 300

    def test_access_phase(self):
        assert Timeouts.ACCESS_PHASE == 180

    def test_worker_spawn(self):
        assert Timeouts.WORKER_SPAWN == 1800

    def test_preparer_spawn(self):
        assert Timeouts.PREPARER_SPAWN == 120

    def test_service_deploy(self):
        assert Timeouts.SERVICE_DEPLOY == 300


class TestCI:
    def test_max_fix_retries(self):
        assert CI.MAX_FIX_RETRIES == 2

    def test_workflow_timeout(self):
        assert CI.WORKFLOW_TIMEOUT == 600

    def test_poll_interval(self):
        assert CI.POLL_INTERVAL == 15

    def test_total_gate_timeout(self):
        assert CI.TOTAL_GATE_TIMEOUT == 3600

    def test_ci_workflow_file(self):
        assert CI.CI_WORKFLOW_FILE == "ci.yml"


class TestProvisioning:
    def test_max_retries(self):
        assert Provisioning.MAX_RETRIES == 3

    def test_password_reset_poll_interval(self):
        assert Provisioning.PASSWORD_RESET_POLL_INTERVAL == 5

    def test_reinstall_poll_interval(self):
        assert Provisioning.REINSTALL_POLL_INTERVAL == 15

    def test_post_reinstall_boot_wait(self):
        assert Provisioning.POST_REINSTALL_BOOT_WAIT == 60

    def test_default_os_template(self):
        assert Provisioning.DEFAULT_OS_TEMPLATE == "kvm-ubuntu-24.04-gpt-x86_64"
