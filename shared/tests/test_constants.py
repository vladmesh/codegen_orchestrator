"""Tests for shared constants."""

from shared.constants import Paths, Timeouts


class TestPaths:
    def test_playbook_helper(self):
        result = Paths.playbook("setup.yml")
        assert result == f"{Paths.ANSIBLE_PLAYBOOKS}/setup.yml"


class TestTimeouts:
    def test_worker_spawn_outlasts_the_turn_it_waits_for(self):
        # The spawn wait is an observer, not a limit. If it could expire first it
        # would take a worker away that is still inside the limit it was given.
        assert Timeouts.WORKER_SPAWN == Timeouts.AGENT_TURN + Timeouts.WORKER_TURN_OVERHEAD
        assert Timeouts.WORKER_SPAWN > Timeouts.AGENT_TURN
