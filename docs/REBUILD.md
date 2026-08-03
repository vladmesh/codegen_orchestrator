# Rebuild

How to rebuild the whole orchestrator without leaving half the system on old code.

## Two independent circuits

The build splits into two parts, and they are not connected to each other:

1. **Compose services.** 20 services in `docker-compose.yml`.
2. **Worker images.** `worker-base-common` and the images derived from it: `worker-base-claude`,
   `worker-base-factory`, `worker-base-codex`. Plus `worker:<tag>` — the images that
   worker-manager builds on the fly for a specific run.

`docker compose build` does not touch the second circuit, and building the workers does not touch the
first. This is the main source of "I rebuilt it and the changes did not get picked up".

## How shared gets delivered

`shared` is not an installable package: it is not installed into the venv, and there is no installed
copy anymore. The only source is the repository tree, and it reaches its consumers through two channels.

**Bind-mount** `./shared:/app/shared` — ten compose services: `api`, `langgraph`,
`deploy-worker`, `qa-worker`, `engineering-worker`, `architect`, `infra-service`, `telegram_bot`,
`scheduler`, `scaffolder`. An edit under `shared/` is picked up by restarting the container
(`docker compose restart <service>`), no image rebuild is needed.

**`COPY shared`** in the Dockerfile — the worker images, the test images and `worker-manager`.
`worker-manager` is the only build service in compose without a mount, so within the compose circuit an
edit to `shared/` requires rebuilding only that one. The worker images live in the second circuit and are
rebuilt according to `WORKER_SOURCE_HASH`.

Locally and in tests `shared` is imported straight from the tree: `scripts/test-unit-local.sh` and
`[tool.pytest.ini_options] pythonpath` keep the repository root on `PYTHONPATH`.

Since `shared` is not installed anywhere, its `dependencies` install nothing: every
consumer has to repeat them in its own `pyproject.toml`. This is watched by
`shared/tests/unit/test_shared_dependency_parity.py` — it finds the consumers itself, by the mounts in
compose and by `COPY shared` in the Dockerfiles.

## Choosing a target

| Target | Service images | Worker images | Volumes and DB | Migrations |
|---|---|---|---|---|
| `make build` | rebuilds | only if the hash is stale | does not touch | no |
| `make rebuild` | rebuilds | always rebuilds | **preserves** | through the `api` entrypoint |
| `make nuke` | rebuilds | checks the hash and fixes it | **wipes** | explicit upgrade + `seed` |
| `make nuke-hard` | `--no-cache` + `builder prune` | the same | **wipes** | explicit upgrade + `seed` |

The usual rebuild after a merge is `make rebuild`. `nuke` is needed only when a genuinely
clean database is required: it removes the volumes `db_data`, `redis_data`, `caddy-config`, `registry-data`.
The `caddy-data` volume with the TLS certificates is preserved by `nuke` deliberately, so as not to pull new
certificates from Let's Encrypt.

Before wiping the database `nuke` calls `infra/scripts/dump-server-keys.sh`, and `make seed` then
restores the servers through `restore-server-keys.sh`. If that step fails, the SSH keys of the
provisioned servers will be lost together with the database.

## What make rebuild does

1. `docker compose down --remove-orphans`.
2. Kills orphaned `worker-*` containers that do not belong to the project.
3. `docker compose build` — all services.
4. `make rebuild-worker-images` — the four base worker images.
5. `docker compose up -d`.

Volumes are not touched, so the database and the registry survive the rebuild.

## Migrations

`services/api/entrypoint.sh` runs `alembic upgrade head` before starting uvicorn. In compose `api`
has `depends_on: db: {condition: service_healthy}`, and `db` has a healthcheck, so on
`up -d` the database is guaranteed to be ready by the time of the migration. There is no need to call
`make migrate` separately after `rebuild`.

Important: `api` has no restart policy. If a migration fails, the container stays down and will not
come up on its own, while the rest of the stack keeps working against the old schema. After a rebuild always
check `docker compose ps api` and `docker compose logs api`.

`make migrate` (`compose exec api alembic upgrade head`) is needed only to apply the schema without
restarting the service.

## Worker images: why a separate mechanism

**The freshness hash.** `WORKER_SOURCE_HASH` in the Makefile is the sha256 of:

- all of `shared/`
- all of `packages/worker-wrapper/`
- all of `services/worker-manager/images/`

(excluding `__pycache__` and `*.pyc`)

This is exactly what the worker Dockerfiles put into the image: `COPY shared /app/shared` copies all of
`shared`, not a subset. Any edit under `shared/` — including `shared/models`,
`shared/clients`, `shared/schemas` — makes the base worker images stale and leads to a
rebuild. The price is accepted deliberately: an extra rebuild is cheaper than a silently outdated image.

**The build order is mandatory.** `worker-base-claude`, `-factory`, `-codex` are declared as
`FROM ${BASE_IMAGE}`, and `BASE_IMAGE` has no default: each producer names the common image it
just produced. `make rebuild-worker-images` tags common as `worker-base-common:$(WORKER_SOURCE_HASH)`
alongside `:latest` and passes the hash tag; the backend integration fixture
(`tests/integration/backend/conftest.py`) passes its own content-hash tag; the e2e fixture passes
the tag it builds one step earlier. A build that forgets the argument fails on a blank base name
rather than layering on whatever `:latest` happens to be on the host.

The backend integration fixture skips a build when the tag already exists in its persistent DinD
volume, so there the tag is the cache key. A child tag is therefore hashed from the child
Dockerfile **and the common image's hash** (`_child_image_hash`): a rebuilt common gives every
child a new tag and a real rebuild. Hashing only the child Dockerfile would leave the old child in
place and retag it `:latest`, and the `BASE_IMAGE` passed to it would never be used. The e2e
fixture builds with `nocache=True` and skips nothing, so it has no such key. All four images get
`--build-arg SOURCE_HASH` and set their own `org.codegen.worker_source_hash` label themselves, rather than
inheriting it from the base. Building a derived image without rebuilding common means getting old code
under the current hash. `make rebuild-worker-images` respects the order; when building by hand, respect it
yourself.

**Derived images are invalidated automatically.** `worker:<tag>` is built by worker-manager at
runtime (`services/worker-manager/src/image_builder.py`). The tag is computed from the capabilities, the
agent_type and the `org.codegen.worker_source_hash` label of the base image, read at build time.
A change to the base code produces a different tag, so a cache hit on an image with old code is impossible and a
manual `docker rmi worker:*` in the make targets is not needed. If the base image has no label,
worker-manager fails with a `RuntimeError` instead of caching unknown code.

**Checking and fixing are separated.**

| Target | What it does |
|---|---|
| `make check-worker-images` | read-only: compares `WORKER_SOURCE_HASH` with the label of each of the four images, prints the mismatch and exits with a non-zero code. Builds nothing. |
| `make ensure-worker-images` | the same check, but on a mismatch it calls `rebuild-worker-images`. |

Other targets (`make build`, `nuke`) call `ensure-worker-images`. `check-worker-images`
is suitable for CI and for manual diagnostics: you can rely on it, it does not mutate the system.

## A clean rebuild after a merge

```bash
cd /home/dev/projects/codegen_orchestrator
git checkout main && git pull

make rebuild

# the schema reached head
docker compose exec -T db psql -U postgres -d orchestrator -tAc \
  "SELECT version_num FROM alembic_version"

# api did not die on the migrations
docker compose ps api
docker compose logs --tail=30 api

# the worker images match the sources
make check-worker-images
```

Expected: `alembic_version` equals the latest revision in
`services/api/migrations/versions/`, `api` is healthy, `check-worker-images` prints
`up to date`.

## Small things that cause confusion

- `--profile build` in the Makefile targets has no effect: there are no profiles in compose,
  `docker compose config --profiles` is empty. The flag is left over from a previous build scheme.
- `make down` not only stops the stack but also removes orphaned `worker-*` containers and the
  `codegen_worker` network.
- `make stop` is an alias of `make down`, not a pause.
- The script `scripts/clean_live_tests.py` reads the `projects` schema directly. After migrations that change
  that table it has to be checked separately: it breaks silently.
- `admin-frontend` builds the nginx config at image build time. If a merge removed or added a
  service that nginx proxies to, a targeted reload is not enough: the container will loop with
  `host not found in upstream` until the image is rebuilt.
- A targeted rebuild after a merge requires checking two places: the changed services, and the fact that compose
  does not pick up changes to the environment variables of an already running container. A container with a new
  environment is recreated in compose only on the next `up -d` of that service.
