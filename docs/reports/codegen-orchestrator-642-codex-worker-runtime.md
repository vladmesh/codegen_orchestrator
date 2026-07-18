# Codex developer-worker report

Task: `codegen_orchestrator-642`
Date: 2026-07-18

## Result

Codex is a strict `AgentType` value carried through project config, LangGraph,
the existing worker command DTO, worker-manager, the agent-specific image, and
worker-wrapper. The queue envelope is unchanged. Unknown agent types fail at
the typed or explicit routing boundary.

The runner keeps filesystem writes in the `workspace-write` sandbox and
enables sandboxed network commands so the agent can reach the localhost result
bridge, dependencies, and Git remote. Optional API-key mode uses the CLI's
`CODEX_API_KEY`; host-session mode sets neither API-key variable.

`auth_mode=host_session` requires a separate `HOST_CODEX_HOME` with directory
mode `0700`, `auth.json` and `config.toml` mode `0600`, a non-empty JSON session,
refresh-capable access and refresh tokens, and
`cli_auth_credentials_store = "file"`. The profile is mounted read-write
only into Codex workers at `/home/worker/.codex`; `CODEX_HOME` points there and
no API key is required.

## Verification

```text
make test-unit
Passed: 8
Failed: 0
All unit tests passed!

make lint
All checks passed!

uv run pytest packages/worker-wrapper/tests/component -q
7 passed

docker compose config --quiet --no-env-resolution
exit 0

uv run pytest shared/tests/unit/test_codex_deployment_contract.py -q
1 passed

Production deploy writes `HOST_CODEX_HOME` and pulls
`worker-base-common`, `worker-base-claude`, `worker-base-factory`, and
`worker-base-codex` from GHCR before starting the stack.

make rebuild-worker-images
worker-base-common:latest built
worker-base-claude:latest built
worker-base-factory:latest built
worker-base-codex:latest built
Worker images rebuilt

make check-worker-images
Worker base images up to date

docker run --rm --entrypoint sh worker-base-codex:latest -c \
  'set -eu; test "$(id -un)" = worker; test "$CODEX_HOME" = /home/worker/.codex; \
  test -d "$CODEX_HOME"; test -w "$CODEX_HOME"; codex --version'
codex-cli 0.144.6
user=worker codex_home=/home/worker/.codex writable=yes

docker run --rm worker-base-codex:latest healthcheck
Healthcheck passed

docker network create codex-smoke-642
docker run -d --name codex-redis-smoke-642 --network codex-smoke-642 redis:7-alpine
docker run -d --name codex-worker-smoke-642 --network codex-smoke-642 \
  -e WORKER_REDIS_URL=redis://codex-redis-smoke-642:6379/0 \
  -e WORKER_INPUT_STREAM=codex:smoke:input \
  -e WORKER_OUTPUT_STREAM=codex:smoke:output \
  -e WORKER_CONSUMER_GROUP=codex-smoke \
  -e WORKER_CONSUMER_NAME=codex-smoke-642 \
  -e WORKER_AGENT_TYPE=codex worker-base-codex:latest
docker logs codex-worker-smoke-642 | rg worker_wrapper_starting
docker exec codex-worker-smoke-642 worker-wrapper healthcheck
Healthcheck passed
wrapper=running agent=codex user=worker codex_home=/home/worker/.codex
```

The wrapper smoke used an isolated Redis container, no real session material,
and printed no credentials. Both temporary containers and their Docker network
were removed after the check.
