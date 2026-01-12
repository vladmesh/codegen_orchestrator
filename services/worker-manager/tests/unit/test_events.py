import pytest
from unittest.mock import MagicMock, AsyncMock
from src.events import DockerEventsListener


class TestDockerEventsListener:
    @pytest.mark.asyncio
    async def test_detects_worker_container_die_event(self):
        """Should detect when worker container dies."""
        mock_redis_client = MagicMock()
        mock_redis_client.xadd = AsyncMock()

        # Redis mapping simulation (worker_id -> task info)
        # In real impl we might need to look this up, or labels
        # For this test assuming we parse labels from event or lookup

        listener = DockerEventsListener(mock_redis_client)

        # Simulate Docker event for a worker container dying
        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "worker-test-123",
                    "exitCode": "137",
                    "com.docker.compose.service": "worker-manager",  # Just noise
                }
            },
        }

        # We need a way to mock the lookup of task_id for this worker
        # Assuming the listener has a method or way to resolve this.
        # For simplicity, let's assume labels on the container have the info
        # normally, but events give us attributes.

        # Let's say we expect the listener to extract info and publish
        # But wait, how does it know the task_id?
        # The design says "Extract task_id (from labels or Redis mapping)".
        # Let's assume we rely on Redis mapping for now as labels might not remain if container gone?
        # Actually 'die' event attributes contain the container labels at time of death usually.
        # Let's update the event mock to include labels in attributes

        event["Actor"]["Attributes"]["label_task_id"] = "task-123"
        event["Actor"]["Attributes"]["label_worker_type"] = "developer"

        await listener._handle_event(event)

        mock_redis_client.xadd.assert_called_once()
        args, kwargs = mock_redis_client.xadd.call_args
        stream_key = args[0]
        message = args[1]

        assert stream_key == "worker:developer:output"
        assert message["task_id"] == "task-123"
        assert "crashed" in message["content"]
        assert "137" in message["content"]

    @pytest.mark.asyncio
    async def test_ignores_non_worker_containers(self):
        """Should ignore containers not matching worker-* pattern or missing labels."""
        mock_redis_client = MagicMock()
        mock_redis_client.xadd = AsyncMock()

        listener = DockerEventsListener(mock_redis_client)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {"Attributes": {"name": "postgres_db", "exitCode": "0"}},
        }

        await listener._handle_event(event)
        mock_redis_client.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_zero_exit_code(self):
        """Should ignore successful exits."""
        mock_redis_client = MagicMock()
        mock_redis_client.xadd = AsyncMock()

        listener = DockerEventsListener(mock_redis_client)

        event = {
            "Type": "container",
            "Action": "die",
            "Actor": {
                "Attributes": {
                    "name": "worker-good",
                    "exitCode": "0",
                    "label_task_id": "t-1",
                    "label_worker_type": "dev",
                }
            },
        }

        await listener._handle_event(event)
        mock_redis_client.xadd.assert_not_called()
