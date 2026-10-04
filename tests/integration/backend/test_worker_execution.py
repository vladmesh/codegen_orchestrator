import hashlib
import json
from pathlib import Path
from uuid import uuid4

from docker.errors import NotFound
import pytest

from shared.contracts.dto.engineering_execution import (
    EngineeringExecutionEvidence,
    EngineeringExecutionPhase,
    EngineeringInfrastructureRefusal,
)
from shared.contracts.dto.worker import WorkerStatus, worker_creation_failure_key
from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerChannels,
    WorkerConfig,
)
from shared.contracts.queues.worker_result import WorkerResultStatus, parse_worker_result
from shared.contracts.worker_turn import WorkerTurnInput, active_turn_key

from .conftest import (
    REDIS_STREAM_COMMANDS,
    REDIS_STREAM_DEV_RESPONSES,
    WORKSPACE_BASE_PATH,
    scaffolded_worker_config,
    wait_for_create_response,
    wait_for_stream_message,
)
from .worker_authority import publish_worker_fixture_turn


@pytest.mark.integration
@pytest.mark.asyncio
class TestWorkerExecution:
    @pytest.mark.parametrize("invalid", ["missing", "foreign", "stale", "stopped"])
    async def test_invalid_attempt_refused_before_container_or_materials(
        self,
        api_client,
        redis_client,
        docker_client,
        scaffolded_workspace,
        worker_authority,
        test_worker_owners,
        invalid,
    ):
        owner = await worker_authority()
        if invalid == "missing":
            owner = owner.model_copy(update={"attempt_id": f"absent-{uuid4().hex}"})
        elif invalid == "foreign":
            neighbour = await worker_authority()
            owner = owner.model_copy(update={"project_id": neighbour.project_id})
        elif invalid == "stale":
            owner = owner.model_copy(update={"run_id": f"stale-{uuid4().hex}"})
        else:
            response = await api_client.post(f"/api/stories/{owner.story_id}/fail", json={})
            response.raise_for_status()
        test_worker_owners.append(owner)  # Explicit negative identity remains test-owned cleanup.
        before = await api_client.get("/api/runs/", params={"project_id": owner.project_id})
        before.raise_for_status()
        worker_id = f"authority-refused-{uuid4().hex[:12]}"
        request_id = f"authority-{uuid4().hex[:12]}"
        command = CreateWorkerCommand(
            request_id=request_id,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name=worker_id,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Must remain uninjected.",
                task_content="No model turn.",
                allowed_commands=[],
                capabilities=[],
                ownership=owner,
                auth_mode="api_key",
                api_key="sk-ant-test-claude-key",
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})
        with pytest.raises(RuntimeError):
            await wait_for_create_response(redis_client, REDIS_STREAM_DEV_RESPONSES, request_id)
        failure = await redis_client.hgetall(f"worker:status:{worker_id}")
        error = await redis_client.get(f"worker:error:{worker_id}")
        assert (
            "engineering-disposition" in error
            if invalid == "missing"
            else ("ownership/disposition refused" in error)
        )
        assert failure["execution_phase"] == EngineeringExecutionPhase.PRE_AGENT_REFUSED
        with pytest.raises(NotFound):
            docker_client._test_direct_container_get(f"worker-{worker_id}")
        workspace = Path(WORKSPACE_BASE_PATH, scaffolded_workspace)
        for path in ("TASK.md", "CLAUDE.md", ".git-credentials", ".config/gh/hosts.yml"):
            assert not (workspace / path).exists()
        after = await api_client.get("/api/runs/", params={"project_id": owner.project_id})
        after.raise_for_status()
        assert after.json() == before.json()  # Worker refusal admits no additional Run or spend.

    @pytest.mark.asyncio
    async def test_create_claude_worker_with_git_capability(
        self, redis_client, docker_client, scaffolded_workspace, worker_authority
    ):
        """
        Scenario D.1: Create Claude worker with GIT capability.
        """
        req_id = f"test-req-{uuid4().hex[:6]}"
        command = CreateWorkerCommand(
            request_id=req_id,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name="test-claude",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="You are a test assistant.",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.GIT, WorkerCapability.GITHUB_CLI],
                ownership=await worker_authority(),
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        assert result.worker_id is not None
        worker_id = result.worker_id

        # Verify container configuration
        container = docker_client.containers.get(f"worker-{worker_id}")

        # Check git is installed
        exit_code, output = container.exec_run("git --version")
        assert exit_code == 0
        assert b"git version" in output

        # Check CLAUDE.md exists with instructions
        exit_code, output = container.exec_run("cat /workspace/CLAUDE.md")
        assert exit_code == 0
        assert b"test assistant" in output

        # Readiness requires real credential preparation, including native HOME
        # configuration usable after the agent's environment filter removes tokens.
        exit_code, output = container.exec_run("cat /workspace/.git/config")
        assert exit_code == 0
        container.reload()
        assert not any(
            item.startswith(("GITHUB_TOKEN=", "GH_TOKEN="))
            for item in container.attrs["Config"]["Env"]
        )
        assert f"https://github.com/{command.config.env_vars['REPO_NAME']}.git".encode() in output

        probe = """import os
from pathlib import Path
import subprocess
home = Path(os.environ["HOME"])
for path in (
    home / ".config/codegen/git-credentials", home / ".git-credentials",
    home / ".config/gh/hosts.yml",
):
    assert not path.exists(), str(path)
config = (home / ".gitconfig").read_text()
assert "/usr/local/bin/git-credential-codegen" in config
assert "useHttpPath = true" in config
assert "store --file" not in config
for operation in ("store", "erase"):
    result = subprocess.run(
        ["/usr/local/bin/git-credential-codegen", operation],
        input="password=synthetic-sentinel\\n\\n",
        text=True, capture_output=True, check=True,
    )
    assert not result.stdout and not result.stderr
assert not (home / ".git-credentials").exists()
# The synthetic repository has no platform ownership record. The actual shipped
# helper must refuse it through the broker, with a payload-free diagnostic.
query = f"protocol=https\\nhost=github.com\\npath={os.environ['REPO_NAME']}.git\\n\\n"
result = subprocess.run(
    ["/usr/local/bin/git-credential-codegen", "get"], input=query,
    text=True, capture_output=True, timeout=35,
)
assert result.returncode != 0 and not result.stdout
assert result.stderr == "repository credential unavailable\\n"
for entrypoint in ("gh", "/usr/bin/gh"):
    version = subprocess.run([entrypoint, "--version"], capture_output=True, check=True)
    assert b"gh version" in version.stdout
    refused = subprocess.run([entrypoint, "api", "user"], capture_output=True, timeout=35)
    assert refused.returncode != 0 and not refused.stdout
    assert refused.stderr == b"repository credential unavailable\\n"
assert not (home / ".config/gh/hosts.yml").exists()
"""
        exit_code, output = container.exec_run(["python3", "-c", probe])
        assert exit_code == 0, f"Native repository credential setup failed: {output.decode()}"

    @pytest.mark.parametrize(
        "env_vars",
        [{}, {"GITHUB_TOKEN": "synthetic-invalid"}],
    )
    async def test_missing_repository_credentials_refused_before_injection(
        self, redis_client, scaffolded_workspace, env_vars, worker_authority
    ):
        worker_id = f"missing-credentials-{uuid4().hex[:8]}"
        request_id = f"refused-{uuid4().hex[:8]}"
        # Deliberately bypass the valid fixture producer: these inputs must remain invalid.
        command = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_id,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Must not be injected before credential preparation.",
                task_content="Must not reach an agent.",
                allowed_commands=[],
                capabilities=[],
                ownership=await worker_authority(),
                repo_id=scaffolded_workspace,
                env_vars=env_vars,
                auth_mode="api_key",
                api_key="sk-ant-test-claude-key",
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})
        with pytest.raises(RuntimeError):
            await wait_for_create_response(
                redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=request_id
            )
        failure = await redis_client.hgetall(worker_creation_failure_key(worker_id))
        assert "Repository identity is required" in failure["error"]
        assert (
            await redis_client.hget(f"worker:status:{worker_id}", "status") != WorkerStatus.RUNNING
        )
        workspace = Path(WORKSPACE_BASE_PATH, scaffolded_workspace)
        assert not (workspace / "TASK.md").exists()
        assert not (workspace / "CLAUDE.md").exists()

    @pytest.mark.asyncio
    async def test_create_factory_worker_with_curl_capability(
        self, redis_client, docker_client, scaffolded_workspace, worker_authority
    ):
        """
        Scenario D.2: Create Factory worker with CURL capability.
        """
        req_id = f"test-req-{uuid4().hex[:6]}"
        command = CreateWorkerCommand(
            request_id=req_id,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name="test-factory",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="You are a Factory assistant.",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.CURL],
                ownership=await worker_authority(),
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )

        assert result.success is True, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id

        container = docker_client.containers.get(f"worker-{worker_id}")

        # Codex reads its instructions from WORKER_INSTRUCTIONS.md, never from the
        # product's own tracked AGENTS.md, and never from CLAUDE.md.
        exit_code, _ = container.exec_run("cat /workspace/WORKER_INSTRUCTIONS.md")
        assert exit_code == 0

        exit_code, _ = container.exec_run("ls /workspace/CLAUDE.md")
        assert exit_code != 0  # Should NOT exist

        # Check curl installed
        exit_code, _ = container.exec_run("curl --version")
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_different_agent_types_produce_different_images(
        self, redis_client, docker_client, scaffolded_workspace, worker_authority
    ):
        """
        Scenario B: Image caching respects agent_type.
        """
        # Create Claude worker
        req_id_1 = f"cache-1-{uuid4().hex[:6]}"
        cmd1 = CreateWorkerCommand(
            request_id=req_id_1,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name="cache-claude",
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="test",
                allowed_commands=[],
                capabilities=[WorkerCapability.GIT],
                ownership=await worker_authority(),
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd1.model_dump_json()})
        result1 = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id_1
        )
        assert result1.success, f"Worker 1 creation failed: {result1.error}"
        worker1_id = result1.worker_id
        assert worker1_id == "cache-claude", f"Unexpected worker1_id: {worker1_id}"

        # Create Factory worker with the same pre-scaffolded workspace.
        req_id_2 = f"cache-2-{uuid4().hex[:6]}"
        cmd2 = CreateWorkerCommand(
            request_id=req_id_2,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name="cache-factory",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="test",
                allowed_commands=[],
                capabilities=[WorkerCapability.GIT],
                ownership=await worker_authority(),
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd2.model_dump_json()})
        result2 = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id_2
        )
        assert result2.success, f"Worker 2 creation failed: {result2.error}"
        worker2_id = result2.worker_id
        assert worker2_id == "cache-factory", f"Unexpected worker2_id: {worker2_id}"
        assert worker1_id != worker2_id, "Worker IDs must be different"

        # Get container images
        container1 = docker_client.containers.get(f"worker-{worker1_id}")
        container2 = docker_client.containers.get(f"worker-{worker2_id}")

        # Image TAGS should be DIFFERENT (different agent_type affects hash)
        tags1 = container1.image.tags
        tags2 = container2.image.tags

        # Extract the worker:hash tag
        worker_tag1 = [t for t in tags1 if t.startswith("worker:")]
        worker_tag2 = [t for t in tags2 if t.startswith("worker:")]

        assert worker_tag1, f"No worker tag found for container1: {tags1}"
        assert worker_tag2, f"No worker tag found for container2: {tags2}"
        assert worker_tag1[0] != worker_tag2[0], (
            f"Tags should differ: {worker_tag1[0]} vs {worker_tag2[0]}"
        )

    @pytest.mark.asyncio
    async def test_worker_executes_task_with_mocked_claude(
        self, api_client, redis_client, docker_client, scaffolded_workspace, worker_authority
    ):
        """
        A no-model task reaches the shipped wrapper and its repository refusal.
        A local CLI stub also prevents spend if a preflight regression launches it.
        """
        req_id = f"exec-test-{uuid4().hex[:6]}"

        # 1. Create Worker (Factory Agent for simplicity or Claude)
        command = CreateWorkerCommand(
            request_id=req_id,
            config=scaffolded_worker_config(
                scaffolded_workspace,
                name="exec-worker",
                worker_type="developer",
                agent_type=AgentType.FACTORY,
                instructions="Echo test agent",
                allowed_commands=["project.get"],
                capabilities=[WorkerCapability.CURL],
                ownership=await worker_authority(),
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})

        # 2. Get Worker ID
        result = await wait_for_create_response(
            redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=req_id
        )
        assert result.success, f"Worker creation failed: {result.error}"
        worker_id = result.worker_id
        container = docker_client.containers.get(f"worker-{worker_id}")
        stub = "#!/bin/sh\ntouch /workspace/cli-started\nexit 1\n"
        exit_code, output = container.exec_run(
            [
                "python3",
                "-c",
                "from pathlib import Path; "
                f"p=Path('/usr/local/bin/droid'); p.write_text({stub!r}); p.chmod(0o755)",
            ],
            user="root",
        )
        assert exit_code == 0, output.decode()

        # 3. Start the admitted attempt and bind the ready manager-created worker.
        input_stream = WorkerChannels.INPUT_PATTERN.value.format(worker_id=worker_id)
        turn, stream_id = await publish_worker_fixture_turn(
            api_client,
            command.config.ownership,
            worker_id,
            request_id=f"turn-{req_id}",
            prompt="Hello World",
            turn_deadline_seconds=60,
        )
        inputs = await redis_client.xrange(input_stream)
        assert len(inputs) == 1 and inputs[0][0] == stream_id
        assert WorkerTurnInput.model_validate_json(inputs[0][1]["data"]) == turn

        # 5. Wait for the typed worker result on the output stream.
        # The worker publishes a WorkerResult (completed/failed/blocked/rejected) — even a
        # failed execution writes a terminal result here. This is the worker output
        # contract; the old `worker:lifecycle` stream was removed.
        output_stream = WorkerChannels.OUTPUT_PATTERN.value.format(worker_id=worker_id)
        output_msg = await wait_for_stream_message(redis_client, output_stream, timeout=60)
        output_data = json.loads(output_msg["data"])
        assert output_data["status"] in {s.value for s in WorkerResultStatus}
        typed_result = parse_worker_result(output_data)
        assert typed_result.execution == EngineeringExecutionEvidence(
            execution_phase=EngineeringExecutionPhase.PRE_AGENT_REFUSED,
            infrastructure_refusal=EngineeringInfrastructureRefusal.REPOSITORY_AUTH_UNAVAILABLE,
        )
        # The broker owns request correlation, output receipt and input ACK.
        # A manager-generated container-death result cannot satisfy these proofs.
        assert output_msg["request_id"] == turn.request_id
        assert await redis_client.xlen(output_stream) == 1
        assert (
            await redis_client.get(f"worker:output-receipt:{worker_id}:{stream_id}")
            == hashlib.sha256(typed_result.model_dump_json().encode()).hexdigest()
        )
        broker = await redis_client.hgetall(f"worker:broker:{worker_id}")
        pending = await redis_client.xpending(input_stream, broker["consumer_group"])
        assert pending["pending"] == 0
        groups = await redis_client.xinfo_groups(input_stream)
        group = next(g for g in groups if g["name"] == broker["consumer_group"])
        assert group["last-delivered-id"] == stream_id
        assert not await redis_client.hgetall(active_turn_key(worker_id))
        assert not Path(WORKSPACE_BASE_PATH, scaffolded_workspace, "cli-started").exists()
