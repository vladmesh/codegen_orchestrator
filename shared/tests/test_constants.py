"""Tests for shared constants."""

from shared.constants import Paths, Provisioning, Timeouts


class TestPaths:
    def test_ansible_playbooks_default(self):
        assert Paths.ANSIBLE_PLAYBOOKS == "/app/ansible/playbooks"

    def test_playbook_helper(self):
        result = Paths.playbook("setup.yml")
        assert result == f"{Paths.ANSIBLE_PLAYBOOKS}/setup.yml"


class TestTimeouts:
    def test_provisioning(self):
        assert Timeouts.PROVISIONING == 1200

    def test_reinstall(self):
        assert Timeouts.REINSTALL == 900

    def test_password_reset(self):
        assert Timeouts.PASSWORD_RESET == 300

    def test_access_phase(self):
        assert Timeouts.ACCESS_PHASE == 180

    def test_agent_turn(self):
        assert Timeouts.AGENT_TURN == 3600

    def test_worker_spawn_outlasts_the_turn_it_waits_for(self):
        # The spawn wait is an observer, not a limit. If it could expire first it
        # would take a worker away that is still inside the limit it was given.
        assert Timeouts.WORKER_SPAWN == Timeouts.AGENT_TURN + Timeouts.WORKER_TURN_OVERHEAD
        assert Timeouts.WORKER_SPAWN > Timeouts.AGENT_TURN

    def test_service_deploy(self):
        assert Timeouts.SERVICE_DEPLOY == 300


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
