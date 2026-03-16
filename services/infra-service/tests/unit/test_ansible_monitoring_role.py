"""Tests for the monitoring Ansible role structure and YAML validity."""

from pathlib import Path

import yaml

ROLES_DIR = Path(__file__).parents[2] / "ansible" / "roles"
MONITORING_DIR = ROLES_DIR / "monitoring"
PLAYBOOKS_DIR = Path(__file__).parents[2] / "ansible" / "playbooks"


class TestMonitoringRoleStructure:
    """Verify monitoring role directory structure exists."""

    def test_tasks_main_exists(self):
        assert (MONITORING_DIR / "tasks" / "main.yml").is_file()

    def test_defaults_main_exists(self):
        assert (MONITORING_DIR / "defaults" / "main.yml").is_file()

    def test_handlers_main_exists(self):
        assert (MONITORING_DIR / "handlers" / "main.yml").is_file()


class TestMonitoringDefaults:
    """Verify defaults/main.yml has expected variables."""

    def setup_method(self):
        with open(MONITORING_DIR / "defaults" / "main.yml") as f:
            self.defaults = yaml.safe_load(f)

    def test_is_valid_yaml_dict(self):
        assert isinstance(self.defaults, dict)

    def test_monitoring_enabled_defined(self):
        assert "monitoring_enabled" in self.defaults

    def test_node_exporter_port_defined(self):
        assert self.defaults.get("node_exporter_port") == 9100

    def test_cadvisor_port_defined(self):
        assert self.defaults.get("cadvisor_port") == 8080


class TestMonitoringTasksYaml:
    """Verify tasks/main.yml is valid and has required tasks."""

    def setup_method(self):
        with open(MONITORING_DIR / "tasks" / "main.yml") as f:
            self.tasks = yaml.safe_load(f)

    def test_is_valid_yaml_list(self):
        assert isinstance(self.tasks, list)
        assert len(self.tasks) > 0

    def test_all_tasks_have_name(self):
        for task in self.tasks:
            assert "name" in task, f"Task missing 'name': {task}"

    def test_node_exporter_present(self):
        task_names = " ".join(t["name"].lower() for t in self.tasks)
        assert "node_exporter" in task_names or "node-exporter" in task_names

    def test_cadvisor_present(self):
        task_names = " ".join(t["name"].lower() for t in self.tasks)
        assert "cadvisor" in task_names

    def test_ufw_rules_present(self):
        """UFW rules should restrict monitoring ports to orchestrator IP only."""
        ufw_tasks = [t for t in self.tasks if "ufw" in t.get("name", "").lower()]
        assert len(ufw_tasks) >= 2, f"Expected at least 2 UFW tasks, found {len(ufw_tasks)}"

    def test_ufw_rules_use_orchestrator_ip(self):
        """UFW rules must use orchestrator_ip variable for source restriction."""
        ufw_tasks = [t for t in self.tasks if "ufw" in t.get("name", "").lower()]
        for task in ufw_tasks:
            ufw_config = task.get("ufw", {})
            from_src = ufw_config.get("from_ip", "") or ufw_config.get("from", "")
            assert "orchestrator_ip" in str(from_src), (
                f"UFW task '{task['name']}' doesn't use orchestrator_ip: {ufw_config}"
            )

    def test_ufw_rules_reference_port_vars(self):
        """UFW rules should reference port variables, not hardcoded values."""
        ufw_tasks = [t for t in self.tasks if "ufw" in t.get("name", "").lower()]
        port_refs = " ".join(str(t.get("ufw", {}).get("port", "")) for t in ufw_tasks)
        assert "node_exporter_port" in port_refs or "cadvisor_port" in port_refs, (
            f"UFW tasks should reference port variables: {port_refs}"
        )

    def test_docker_compose_has_cadvisor(self):
        """The docker-compose content must include cadvisor service."""
        compose_tasks = [
            t for t in self.tasks if "compose" in t.get("name", "").lower() and "copy" in t
        ]
        assert len(compose_tasks) > 0, "No compose file copy task found"
        for task in compose_tasks:
            content = task["copy"].get("content", "")
            assert "cadvisor" in content, "Docker compose content missing cadvisor service"
            assert "node-exporter" in content, (
                "Docker compose content missing node-exporter service"
            )


class TestProvisionSoftwareIncludesMonitoring:
    """Verify provision_software.yml includes the monitoring role."""

    def setup_method(self):
        with open(PLAYBOOKS_DIR / "provision_software.yml") as f:
            self.playbook = yaml.safe_load(f)

    def test_monitoring_section_exists(self):
        """provision_software.yml should include the monitoring role."""
        tasks = self.playbook[0].get("tasks", [])
        task_names = [t.get("name", "") for t in tasks]
        monitoring_tasks = [n for n in task_names if "monitoring" in n.lower()]
        assert len(monitoring_tasks) > 0, (
            f"No monitoring tasks in provision_software.yml. Tasks: {task_names}"
        )

    def test_monitoring_after_docker(self):
        """Monitoring must come after Docker installation (needs Docker to run containers)."""
        tasks = self.playbook[0].get("tasks", [])
        docker_idx = None
        monitoring_idx = None
        for i, t in enumerate(tasks):
            name = t.get("name", "").lower()
            if "docker" in name and "ensure" in name and "started" in name:
                docker_idx = i
            if "monitoring" in name:
                if monitoring_idx is None:
                    monitoring_idx = i

        assert docker_idx is not None, "Docker service start task not found"
        assert monitoring_idx is not None, "Monitoring task not found"
        assert monitoring_idx > docker_idx, (
            f"Monitoring (idx={monitoring_idx}) must come after Docker start (idx={docker_idx})"
        )
