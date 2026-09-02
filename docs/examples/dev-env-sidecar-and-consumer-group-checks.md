# Two invariants rescued from the retired `tests/e2e` contour

The legacy `tests/e2e` contour ran only through `tests/compose/e2e/e2e.yml`, which no make target
and no workflow invoked, and its mechanics no longer match the code (raw dicts on `worker:commands`,
developer workers without `repo_id`, a `<result>`-tag parser, a mock Anthropic server superseded by
`AgentType.NOOP`). It was deleted. Two invariants it was the only place to assert are written down
here as drafts, to be wired into the live suites under a separate issue. Nothing in this file runs:
it sits under `docs/`, so pytest does not collect it and `scripts/check-ci-gate.py` does not
inventory it.

## P1 — `worker:commands` carries a consumer group named `worker_manager`

**Invariant.** The `worker:commands` stream has a consumer group whose name is exactly
`WORKER_MANAGER_GROUP` (`shared/queues.py`), the name `services/worker-manager/src/consumer.py`
reads with. A group under any other name means published lifecycle commands are never claimed.

**Why it is uncovered.** `tests/live/test_health.py::test_consumer_group_exists` parametrizes over
`ENGINEERING_QUEUE`, `SCAFFOLD_QUEUE`, `DEPLOY_QUEUE` and `PO_INPUT_QUEUE` only, and asserts merely
that *some* group exists. The original was
`tests/e2e/test_live_smoke.py::TestHealthChecks::test_worker_manager_consumer_group_exists`.

**Target.** `tests/live/test_health.py`, live layer (`make test-live-smoke`), per docs/TESTING.md.

**Draft** — the `redis` fixture is the redis-cli helper bound to the compose service, as in the
neighbouring tests:

```python
from shared.queues import (
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    PO_INPUT_QUEUE,
    SCAFFOLD_QUEUE,
    WORKER_COMMANDS,
    WORKER_MANAGER_GROUP,
)

# ... extend the existing parametrize list with WORKER_COMMANDS, then:


def test_worker_manager_consumer_group_name(redis):
    """worker-manager claims worker:commands under the name its consumer reads with."""
    try:
        result = redis("XINFO", "GROUPS", WORKER_COMMANDS)
    except RuntimeError:
        pytest.skip(f"Stream {WORKER_COMMANDS} does not exist yet (no messages published)")
    assert WORKER_MANAGER_GROUP in result, (
        f"no {WORKER_MANAGER_GROUP} group on {WORKER_COMMANDS}: {result}"
    )
```

**CI cost.** None beyond the stack the live smoke tier already brings up.

## P2 — a worker's compose sidecar comes up and is reachable by service name

**Invariant.** A developer worker created with a `repo_id` can bring a postgres sidecar up in its
own workspace through the broker (`POST /v1/workers/{name}/infra/compose` with
`{"args": ["up", "-d", "--wait", "db"]}`), the sidecar is reachable from inside the worker container
by its compose service name (`pg_isready -h db`), and deleting the worker removes the
`dev_proj_{worker}` network and the workspace directory.

**Why it is uncovered.** `tests/integration/backend/test_dev_env.py` has only the negative compose
check (`test_compose_rejects_absolute_volumes`) and a delete check that asserts the container is
gone, not the network or the workspace. The original,
`tests/e2e/test_dev_env_smoke.py::test_worker_starts_postgres_and_connects`, is dead as written: it
published a raw dict on `worker:commands` and created a developer worker with no `repo_id`, which
worker-manager now refuses.

**Target.** `tests/integration/backend/test_dev_env.py`, class `TestDevEnvIntegration` — integration
layer, run by `tests/compose/integration/backend-dind.yml` (`make test-integration-backend-dind`,
CI on pushes to `main`).

**Draft** — same fixtures, helpers and broker access as the neighbouring tests in that class
(`_ownership()`, `scaffolded_workspace`, `wait_for_create_response`, the worker's own
`WORKER_BROKER_URL` / `WORKER_BROKER_TOKEN` read off the launched container):

```python
    async def test_worker_starts_postgres_sidecar(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Sidecar up via the broker, reachable by service name, gone after delete."""
        req_id = f"dev-env-{uuid4().hex[:6]}"
        worker_name = f"test-sidecar-{req_id}"

        cmd = CreateWorkerCommand(
            request_id=req_id,
            config=WorkerConfig(
                name=worker_name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="Test sidecar",
                allowed_commands=[],
                capabilities=[],
                ownership=_ownership(),
                repo_id=scaffolded_workspace,
            ),
        )
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": cmd.model_dump_json()})
        result = await wait_for_create_response(redis_client, REDIS_STREAM_DEV_RESPONSES, req_id)
        assert result.success, f"Worker creation failed: {result.error}"

        # Seed the compose file through the shared workspace volume, as the negative test does.
        Path(WORKSPACE_BASE_PATH, scaffolded_workspace, "docker-compose.yml").write_text(
            "services:\n"
            "  db:\n"
            "    image: postgres:16-alpine\n"
            "    environment:\n"
            "      POSTGRES_PASSWORD: testpass\n"
            "    healthcheck:\n"
            '      test: ["CMD-SHELL", "pg_isready -U postgres"]\n'
            "      interval: 2s\n"
            "      retries: 15\n"
        )

        container = docker_client.containers.get(f"worker-{worker_name}")
        container.reload()
        env = dict(i.split("=", 1) for i in container.attrs["Config"]["Env"] if "=" in i)
        async with httpx.AsyncClient(
            base_url=env["WORKER_BROKER_URL"],
            headers={"X-Worker-Broker-Token": env["WORKER_BROKER_TOKEN"]},
            timeout=180,
        ) as client:
            response = await client.post(
                f"/v1/workers/{worker_name}/infra/compose",
                json={"args": ["up", "-d", "--wait", "db"]},
            )
        assert response.status_code == 200, response.text
        assert response.json()["exit_code"] == 0, response.text

        exit_code, output = container.exec_run("pg_isready -h db -p 5432 -U postgres")
        assert exit_code == 0, f"pg_isready failed: {output.decode()}"

        del_cmd = DeleteWorkerCommand(request_id=f"del-{uuid4().hex[:6]}", worker_id=worker_name)
        await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": del_cmd.model_dump_json()})
        # then, within a deadline: container gone, docker_client.networks.get(f"dev_proj_{worker_name}")
        # raises NotFound, and Path(WORKSPACE_BASE_PATH, scaffolded_workspace) no longer exists.
```

**CI cost.** The `postgres:16-alpine` image is pulled inside the DinD daemon on every run of the
backend-dind suite (no layer cache there), plus the `--wait` for the healthcheck: roughly a minute
of added wall time, and the first outbound registry pull that suite makes.
