"""Docker exec environment and developer credential lifetime in a real container."""

import io
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from worker_wrapper.wrapper import build_agent_subprocess_env

from shared.contracts.vocab import AgentType
from src import git_ops
from src.container_config import WorkerContainerConfig
from src.docker_ops import DockerClientWrapper
from src.manager import WorkerManager

BASE_IMAGE = os.environ.get(
    "GIT_CREDENTIAL_TEST_IMAGE", "codegen-orchestrator/worker-manager-test-runner:test"
)


@pytest.mark.asyncio
async def test_native_docker_setup_and_later_git_reacquire_credential():
    docker = DockerClientWrapper()
    image = f"codegen-git-credential-test:{uuid4().hex}"
    definition = f"""FROM {BASE_IMAGE}
USER root
RUN useradd -m -u 1000 worker && mkdir /workspace && chown worker:worker /workspace
USER worker
WORKDIR /workspace
ENTRYPOINT ["sleep", "infinity"]
"""
    await docker._run(
        docker._client.images.build, fileobj=io.BytesIO(definition.encode()), tag=image, rm=True
    )
    container = None
    try:
        manager = WorkerManager(redis=AsyncMock(), docker_client=docker)
        # The broker is external to the Git boundary; no live registration here.
        manager._register_broker_worker = AsyncMock()
        environment = await manager._prepare_worker_env(
            WorkerContainerConfig(
                worker_id="git-service",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                capabilities=["git"],
            ),
            {"GITHUB_TOKEN": "docker-current-harmless-token", "REPO_NAME": "org/repo"},
            None,
        )
        container = await docker.run_container(
            image,
            detach=True,
            network_mode="none",
            environment=environment,
        )
        # This is the exact origin format written by the released manager.
        setup = (
            "git init && git config user.name Fixture && "
            "git config user.email fixture@example.test && "
            "git remote add origin "
            "https://x-access-token:released-harmless-token@github.com/org/repo && "
            "git config core.hooksPath .githooks"
        )
        assert (await docker.exec_in_container(container.id, ["bash", "-c", setup]))[0] == 0
        assert await git_ops.configure_git_credentials(
            docker, container.id, "org/repo", "test-worker"
        )
        attrs = await docker.inspect_container(container.id)
        assert not any(
            value.startswith(("GITHUB_TOKEN=", "GH_TOKEN=")) for value in attrs["Config"]["Env"]
        )
        code, config = await docker.exec_in_container(
            container.id, ["cat", "/workspace/.git/config"]
        )
        assert code == 0 and b"https://github.com/org/repo.git" in config
        assert b"released-harmless-token" not in config
        assert b"hooksPath = .githooks" in config
        assert b"extraheader" not in config and b"helper" not in config
        agent_env = build_agent_subprocess_env(
            {**environment, "HOME": "/home/worker", "PATH": "/usr/local/bin:/usr/bin:/bin"}
        )
        assert "GITHUB_TOKEN" not in agent_env and "GH_TOKEN" not in agent_env
        # Loopback-only controlled broker inside the isolated container. Git runs
        # the installed command helper with the same filtered agent environment.
        consumer = r"""import json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
current = ["synthetic-service-first"]
requests = []
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        assert body == {"repository": "org/repo"}
        assert self.headers["X-Worker-Broker-Token"] == os.environ["WORKER_BROKER_TOKEN"]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"token": current[0]}).encode())
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
env = json.loads(os.environ["CODEGEN_TEST_AGENT_ENV"])
env["WORKER_BROKER_URL"] = f"http://127.0.0.1:{server.server_port}"
query = "protocol=https\nhost=github.com\npath=org/repo.git\n\n"
try:
    for token in ("synthetic-service-first", "synthetic-service-after-expiry"):
        current[0] = token
        result = subprocess.run(["git", "credential", "fill"], input=query, text=True,
                                env=env, capture_output=True, check=True)
        assert f"password={token}\n" in result.stdout
        for operation in ("approve", "reject"):
            subprocess.run(["git", "credential", operation], input=result.stdout + "\n",
                           text=True, env=env, capture_output=True, check=True)
        home = Path(env["HOME"])
        for path in (home / ".config/codegen/git-credentials", home / ".git-credentials",
                     home / ".config/gh/hosts.yml"):
            assert not path.exists()
        config = (home / ".gitconfig").read_text()
        assert token not in config and "useHttpPath = true" in config
    assert len(requests) == 2
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
sys.stdout.write("credential-reacquisition-passed\n")
"""
        code, output = await docker.exec_in_container(
            container.id,
            ["python3", "-I", "-c", consumer],
            environment={"CODEGEN_TEST_AGENT_ENV": json.dumps(agent_env)},
        )
        assert code == 0, output.decode()
        assert output == b"credential-reacquisition-passed\n"
    finally:
        if container is not None:
            await docker.remove_container(container.id, force=True)
        await docker.remove_image(image, force=True)
