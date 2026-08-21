"""Runtime contract for the worker wrapper baked into the common base image."""

from scripts.shared_freshness import source_hash

from .conftest import TREE_ROOT


def test_worker_base_common_imports_wrapper_runtime_shared_dependencies(docker_client):
    """The built image, rather than the checkout, must provide every wrapper import.

    Worker containers get only the intentionally small ``shared`` runtime subset.
    Running this import in the image catches a newly-added wrapper dependency before
    a worker reaches an agent CLI in production.
    """
    common_tag = f"worker-base-common:{source_hash(TREE_ROOT)}"
    output = docker_client.containers.run(
        common_tag,
        command=[
            "-c",
            (
                "from shared.constants import Timeouts; "
                "from worker_wrapper.config import WorkerWrapperConfig; "
                "assert WorkerWrapperConfig.model_fields['subprocess_timeout_seconds'].default "
                "== Timeouts.AGENT_TURN; "
                "print(Timeouts.AGENT_TURN)"
            ),
        ],
        entrypoint="python",
        remove=True,
        user="worker",
    )

    assert output.strip() == b"3600"
