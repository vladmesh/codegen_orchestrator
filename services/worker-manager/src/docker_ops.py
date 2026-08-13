import docker
import asyncio
from typing import Any, Dict, List, Tuple
import structlog
from concurrent.futures import ThreadPoolExecutor

logger = structlog.get_logger()


class DockerClientWrapper:
    """
    Async wrapper around blocking docker-py client.
    Abstracts Docker operations to allow mocking and non-blocking execution.
    """

    def __init__(self, base_url: str | None = None):
        self._client = docker.from_env()
        self._executor = ThreadPoolExecutor(max_workers=5)

    async def _run(self, func, *args, **kwargs):
        """Run blocking function in thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))

    async def run_container(self, image: str, **kwargs) -> Any:
        """Run a container."""
        return await self._run(self._client.containers.run, image, **kwargs)

    async def get_container(self, container_id: str) -> Any:
        """Get a container by ID."""
        return await self._run(self._client.containers.get, container_id)

    async def list_containers(self, filters: Dict[str, Any] | None = None, all: bool = False) -> List[Any]:
        """List containers."""
        return await self._run(self._client.containers.list, all=all, filters=filters)

    async def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Stop a container."""
        container = await self.get_container(container_id)
        await self._run(container.stop, timeout=timeout)

    async def remove_container(
        self,
        container_id: str,
        force: bool = False,
        v: bool = False,
        *,
        verify_attempts: int = 20,
        poll_interval: float = 0.25,
    ) -> None:
        """Remove a container and confirm concurrent removal reaches absence."""
        try:
            container = await self.get_container(container_id)
            try:
                await self._run(container.remove, force=force, v=v)
            except docker.errors.APIError as exc:
                explanation = str(exc.explanation or "")
                if exc.status_code != 409 or "already in progress" not in explanation:
                    raise
        except docker.errors.NotFound:
            return

        for _ in range(verify_attempts):
            try:
                await self.get_container(container_id)
            except docker.errors.NotFound:
                return
            await asyncio.sleep(poll_interval)
        raise RuntimeError(f"container {container_id} still exists after removal wait")

    async def pause_container(self, container_id: str) -> None:
        """Pause a container."""
        container = await self.get_container(container_id)
        await self._run(container.pause)

    async def unpause_container(self, container_id: str) -> None:
        """Unpause a container."""
        container = await self.get_container(container_id)
        await self._run(container.unpause)

    async def inspect_container(self, container_id: str) -> Dict[str, Any]:
        """Inspect a container."""
        # container attrs are cached, need to reload to get fresh status
        container = await self.get_container(container_id)
        # attrs property is already populated, but might be stale?
        # get() calls reload() implicitly? No, container object has .attrs.
        # But get() fetches fresh object.
        return container.attrs

    async def image_exists(self, image: str) -> bool:
        """Check if an image exists locally."""
        try:
            await self._run(self._client.images.get, image)
            return True
        except docker.errors.ImageNotFound:
            return False

    async def get_image_label(self, image: str, label: str) -> str | None:
        """Read a single label from a local image. Returns None if the label is absent."""
        img = await self._run(self._client.images.get, image)
        labels = img.attrs["Config"]["Labels"]
        if not labels:
            return None
        return labels.get(label)

    async def pull_image(self, image: str) -> Any:
        """Pull an image."""
        try:
            return await self._run(self._client.images.pull, image)
        except Exception:
            # Re-raise or handle? For now re-raise
            raise

    async def list_images(self, name: str | None = None, all: bool = False) -> List[Any]:
        """List images."""
        return await self._run(self._client.images.list, name=name, all=all)

    async def remove_image(self, image: str, force: bool = False) -> None:
        """Remove an image."""
        try:
            await self._run(self._client.images.remove, image, force=force)
        except docker.errors.ImageNotFound:
            pass

    async def build_image(self, dockerfile_content: str, tag: str) -> Any:
        """
        Build a Docker image from Dockerfile content.

        Args:
            dockerfile_content: Dockerfile content as string
            tag: Tag for the built image (e.g., "worker:abc123")

        Returns:
            Built image object
        """
        import io

        # Docker SDK expects a file-like object or path
        # We use fileobj with a BytesIO containing the Dockerfile
        dockerfile_bytes = dockerfile_content.encode("utf-8")

        def _build():
            # Create a minimal build context with just the Dockerfile
            import tarfile

            # Build context as tar archive
            context = io.BytesIO()
            with tarfile.open(fileobj=context, mode="w") as tar:
                # Add Dockerfile to the archive
                dockerfile_info = tarfile.TarInfo(name="Dockerfile")
                dockerfile_info.size = len(dockerfile_bytes)
                tar.addfile(dockerfile_info, io.BytesIO(dockerfile_bytes))

            context.seek(0)

            # Build the image
            image, build_logs = self._client.images.build(
                fileobj=context,
                custom_context=True,
                tag=tag,
                rm=True,  # Remove intermediate containers
                forcerm=True,  # Always remove intermediate containers
            )
            return image

        logger.info("building_image", tag=tag)
        return await self._run(_build)

    async def get_container_logs(self, container_id: str, tail: int = 50) -> str:
        """Get recent logs from a container."""
        try:
            container = await self.get_container(container_id)
            logs = await self._run(container.logs, tail=tail)
            return logs.decode(errors="replace")
        except Exception as e:
            return f"Failed to get logs: {e}"

    async def read_container_logs(self, container_id: str, tail: int = 50) -> str:
        """Read a container's logs, raising if they cannot be read.

        `get_container_logs` answers a failed read with the failure text, which
        is fine for a log line and wrong for evidence: a caller that has to say
        whether it captured the tail must be able to tell a log that says
        "Failed to get logs" from a read that failed.
        """
        container = await self.get_container(container_id)
        logs = await self._run(container.logs, tail=tail, stdout=True, stderr=True)
        return logs.decode(errors="replace")

    async def list_networks(self) -> List[Any]:
        """List all Docker networks."""
        return await self._run(self._client.networks.list)

    async def create_network(
        self,
        name: str,
        driver: str = "bridge",
        internal: bool = False,
        labels: Dict[str, str] | None = None,
    ) -> Any:
        """Create a Docker network. `internal` means no route off the network.

        `labels` are the network's ownership, applied at creation for the same
        reason a container's are: they are the only thing that still names the
        owner once Redis has forgotten the worker this network was made for.
        """
        return await self._run(
            self._client.networks.create, name, driver=driver, internal=internal, labels=labels or {}
        )

    async def inspect_network(self, name: str) -> Dict[str, Any]:
        """Read a network's attributes, including whether it is internal."""
        network = await self._run(self._client.networks.get, name)
        return network.attrs

    async def remove_network(self, name: str) -> None:
        """Remove a Docker network, ignoring NotFound."""
        try:
            network = await self._run(self._client.networks.get, name)
            await self._run(network.remove)
        except docker.errors.NotFound:
            pass

    async def connect_network(self, network_name: str, container_id: str, aliases: List[str] | None = None) -> None:
        """Connect a container to a network."""
        network = await self._run(self._client.networks.get, network_name)
        await self._run(network.connect, container_id, aliases=aliases)

    async def disconnect_network(self, network_name: str, container_id: str) -> None:
        """Disconnect a container from a network, ignoring NotFound."""
        try:
            network = await self._run(self._client.networks.get, network_name)
            await self._run(network.disconnect, container_id)
        except docker.errors.NotFound:
            pass

    async def exec_in_container(
        self, container_id: str, command: str, user: str = "worker", timeout: int = 30
    ) -> Tuple[int, bytes]:
        """
        Execute a command in a running container.

        Args:
            container_id: ID of the container
            command: Command run
            user: User to run command as (default: "worker")
            timeout: Timeout in seconds (default: 30)

        Returns:
            Tuple of (exit_code, output_bytes)
        """
        container = await self.get_container(container_id)
        # exec_run is blocking, run in executor
        # returns (exit_code, output)
        return await asyncio.wait_for(self._run(container.exec_run, cmd=command, user=user), timeout=timeout)
