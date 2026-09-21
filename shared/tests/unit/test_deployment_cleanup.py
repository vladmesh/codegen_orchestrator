import shlex

from shared import deployment_cleanup


def test_remote_cleanup_command_keeps_project_name_as_argv() -> None:
    project_name = "live-test'\nrm -rf /"

    command = deployment_cleanup.build_remote_cleanup_command(project_name)

    assert shlex.split(command) == ["sh", "-s", "--", project_name, "/opt/services"]


def test_remote_cleanup_script_is_owned_by_runtime_neutral_module() -> None:
    assert deployment_cleanup.REMOTE_CLEANUP_SCRIPT.name == "deployment_cleanup.sh"
    assert deployment_cleanup.REMOTE_CLEANUP_SCRIPT.is_file()
