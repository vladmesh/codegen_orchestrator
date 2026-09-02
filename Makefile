.PHONY: lint format ci-contract export-env-contract-schema test-unit test-integration test-template-compat test-e2e-scaffold test-live test-live-clean test-live-smoke test-live-engineering test-live-mega test-live-mega-noop test-live-mega-llm test-live-matrix test-live-pipeline test-clean danger-prod-reset stand-preflight stand-run stand-e2e stand-clean \
	build up down stop logs help nuke nuke-hard seed migrate makemigrations \
	setup-hooks lock-deps \
	rebuild-worker-images rebuild-worker-images-hard rebuild \
	check-worker-images ensure-worker-images check-shared-freshness print-source-hash \
	.nuke-common .nuke-hard-prune

# Load .env file
-include .env
export

DOCKER_COMPOSE ?= docker compose

# Hash of source files baked into images: all of shared/ (COPY shared ./shared), all of
# packages/worker-wrapper, and the worker image definitions themselves. Any edit under
# shared/ therefore makes every image that bakes it stale.
# Computed by scripts/shared_freshness.py and nowhere else — the freshness check reads the
# same function, so the two cannot drift apart.
WORKER_SOURCE_HASH := $(shell python3 scripts/shared_freshness.py hash)

COMPOSE_ENV := HOST_UID=$$(id -u) HOST_GID=$$(id -g)

# Test Project Name for Isolation
TEST_PROJECT := codegen_orchestrator_test

# Default target
help:
	@echo "Available commands:"
	@echo "  make up          - Start all services"
	@echo "  make down        - Stop all services"
	@echo "  make logs        - View logs"
	@echo "  make build       - Build all images"
	@echo ""
	@echo "  make lint        - Run linters"
	@echo "  make format      - Format code"
	@echo ""
	@echo "Testing:"
	@echo "  make test-unit            - Run all unit tests (fast)"
	@echo "  make test-service SERVICE=name - Run service tests for a specific module"
	@echo "  make test-integration     - Run all integration tests"
	@echo "  make test-live            - Run all live tests (from host, no LLM)"
	@echo "  make test-live N=health   - Run specific live test file"
	@echo "  make test-live-mega-noop  - Run only the free noop full-pipeline class"
	@echo "  make test-live-mega-llm   - Run only the one-pair LLM full-pipeline class"
	@echo "  make test-live-matrix     - Run four LLM pairs through the stand runner"
	@echo "  make test-e2e-scaffold    - Run scaffolding E2E tests"
	@echo "  make test-clean           - Cleanup test containers"
	@echo ""
	@echo "Git Hooks:"
	@echo "  make setup-hooks  - Install git hooks (format check, tests)"
	@echo ""
	@echo "  make migrate     - Run database migrations"
	@echo "  make makemigrations MSG='...' - Create new migration"
	@echo ""
	@echo "  make nuke           - Full reset: clean workers, remove volumes, incremental rebuild"
	@echo "  make nuke-hard      - Full reset: clean workers, remove volumes, NO-CACHE rebuild"
	@echo "  make seed           - Seed database with API keys from env"
	@echo "  make lock-deps      - Regenerate all requirements.lock files"
	@echo ""
	@echo "  make rebuild      - Rebuild everything (services + worker images), restart stack"
	@echo ""
	@echo "Worker Images:"
	@echo "  make rebuild-worker-images      - Rebuild worker base images (common → claude/factory/codex)"
	@echo "  make rebuild-worker-images-hard - Rebuild with --no-cache (when cache is stale)"
	@echo "  make check-worker-images        - Read-only staleness check (non-zero exit on drift)"
	@echo "  make ensure-worker-images       - Check and rebuild if stale"
	@echo ""
	@echo "  make check-shared-freshness     - Is anything built behind the tree on shared?"

# === Dependency Lock Files ===

lock-deps:
	@echo "🔒 Generating requirements.lock files with uv..."
	uv pip compile services/langgraph/pyproject.toml -o services/langgraph/requirements.lock
	uv pip compile services/api/pyproject.toml -o services/api/requirements.lock
	uv pip compile services/scheduler/pyproject.toml -o services/scheduler/requirements.lock
	uv pip compile services/telegram_bot/pyproject.toml -o services/telegram_bot/requirements.lock
	uv pip compile services/worker-manager/pyproject.toml -o services/worker-manager/requirements.lock
	uv pip compile services/infra-service/pyproject.toml -o services/infra-service/requirements.lock
	uv pip compile services/scaffolder/pyproject.toml -o services/scaffolder/requirements.lock
	@echo "✅ All lock files updated!"

# === Docker ===

up:
	$(DOCKER_COMPOSE) up -d 

down:
	@docker ps -a --filter "name=worker-" --format "{{.Names}}" | grep -v "codegen_orchestrator" | xargs -r docker rm -f 2>/dev/null || true
	$(DOCKER_COMPOSE) down --remove-orphans
	@docker network rm codegen_worker 2>/dev/null || true

stop: down

logs:
	$(DOCKER_COMPOSE) logs -f

build:
	$(DOCKER_COMPOSE) --profile build build
	@$(MAKE) ensure-worker-images

# === Full Rebuild (with cache) ===
# Stops stack, kills workers, rebuilds all service + worker images, restarts
rebuild:
	@echo "🔄 Rebuilding everything..."
	@echo "🛑 Stopping stack..."
	$(DOCKER_COMPOSE) down --remove-orphans
	@echo "🔪 Killing worker containers..."
	@docker ps -a --filter "name=worker-" --format "{{.Names}}" | grep -v "codegen_orchestrator" | xargs -r docker rm -f 2>/dev/null || true
	@echo "🔨 Building service images..."
	$(DOCKER_COMPOSE) --profile build build
	@echo "🔨 Building worker base images..."
	@$(MAKE) rebuild-worker-images
	@echo "🚀 Starting stack..."
	$(DOCKER_COMPOSE) up -d
	@echo "✅ Rebuild complete!"

# === Worker Base Images ===
# Build the worker image chain: common -> claude/factory/codex
# Use rebuild-worker-images after changing worker-wrapper or worker-base Dockerfiles
#
# common is tagged with the source hash as well as :latest, and the children here are
# built against that hash tag, so they are layered on the common this target just built.
# Their Dockerfiles declare BASE_IMAGE without a default, so a build that forgets to name
# a base fails instead of picking up a stray :latest. The other producer of these images,
# the DinD fixture in tests/integration/backend/conftest.py, names its own tag the same
# way; it skips a build when the tag exists, so its child tags carry the common hash.

rebuild-worker-images:
	@echo "🔨 Building worker-base-common..."
	docker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		-t worker-base-common:latest \
		-t worker-base-common:$(WORKER_SOURCE_HASH) \
		-f services/worker-manager/images/worker-base-common/Dockerfile .
	@echo "🔨 Building worker-base-claude..."
	docker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-claude:latest \
		-f services/worker-manager/images/worker-base-claude/Dockerfile .
	@echo "🔨 Building worker-base-factory..."
	docker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-factory:latest \
		-f services/worker-manager/images/worker-base-factory/Dockerfile .
	@echo "🔨 Building worker-base-codex..."
	docker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-codex:latest \
		-f services/worker-manager/images/worker-base-codex/Dockerfile .
	@echo "✅ Worker images rebuilt!"

# Full rebuild with --no-cache (use when Docker cache is stale)
rebuild-worker-images-hard:
	@echo "🔨 Building worker-base-common (no-cache)..."
	docker build --no-cache --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		-t worker-base-common:latest \
		-t worker-base-common:$(WORKER_SOURCE_HASH) \
		-f services/worker-manager/images/worker-base-common/Dockerfile .
	@echo "🔨 Building worker-base-claude (no-cache)..."
	docker build --no-cache --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-claude:latest \
		-f services/worker-manager/images/worker-base-claude/Dockerfile .
	@echo "🔨 Building worker-base-factory (no-cache)..."
	docker build --no-cache --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-factory:latest \
		-f services/worker-manager/images/worker-base-factory/Dockerfile .
	@echo "🔨 Building worker-base-codex (no-cache)..."
	docker build --no-cache --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		--build-arg BASE_IMAGE=worker-base-common:$(WORKER_SOURCE_HASH) \
		-t worker-base-codex:latest \
		-f services/worker-manager/images/worker-base-codex/Dockerfile .
	@echo "✅ Worker images rebuilt (no-cache)!"

# Read-only staleness check. Builds nothing, exits non-zero on drift.
check-worker-images:
	@CURRENT=$(WORKER_SOURCE_HASH); \
	STALE=""; \
	for IMAGE in worker-base-common worker-base-claude worker-base-factory worker-base-codex; do \
	  STORED=$$(docker inspect "$$IMAGE:latest" \
	    --format '{{index .Config.Labels "org.codegen.worker_source_hash"}}' 2>/dev/null || echo "missing"); \
	  if [ "$$CURRENT" != "$$STORED" ]; then STALE="$$STALE $$IMAGE($$STORED)"; fi; \
	done; \
	if [ -n "$$STALE" ]; then \
	  echo "⚠️  Worker base images stale (expected $$CURRENT):$$STALE"; \
	  echo "   Fix with: make ensure-worker-images"; \
	  exit 1; \
	fi; \
	echo "✅ Worker base images up to date (hash: $$CURRENT)"

# Is anything built behind the tree on shared? Covers every image that bakes shared,
# the worker bases included; unlike check-worker-images an image that is not built is
# not a failure, so this is the one that runs in CI. Builds nothing, needs no network.
check-shared-freshness:
	@uv run python scripts/shared_freshness.py check

# The tree hash, for anything that needs to compare against a built image.
print-source-hash:
	@echo $(WORKER_SOURCE_HASH)

# Check and rebuild on drift. This is what other targets call.
ensure-worker-images:
	@if $(MAKE) --no-print-directory check-worker-images 2>/dev/null; then \
	  :; \
	else \
	  $(MAKE) rebuild-worker-images; \
	fi

# === Quality ===

lint:
	@uv run ruff format --check $(if $(LINT_PATH),$(LINT_PATH),.)
	@uv run ruff check $(if $(LINT_PATH),$(LINT_PATH),.)

format:
	@uv run ruff format $(if $(FILES),$(FILES),.) && uv run ruff check --fix $(if $(FILES),$(FILES),.)

ci-contract:
	@uv run python scripts/check-ci-gate.py

export-env-contract-schema:
	@PYTHONPATH=. uv run python scripts/export-env-contract-schema.py

# === Git Hooks ===

setup-hooks:
	@echo "🔧 Installing git hooks..."
	@chmod +x .githooks/pre-commit .githooks/pre-push scripts/setup-hooks.sh
	@bash scripts/setup-hooks.sh


# === Testing ===

# Integration tests - pattern rule for dynamic discovery
# Any tests/compose/integration/*.yml file automatically becomes test-integration-* target
INTEGRATION_COMPOSE_FILES := $(wildcard tests/compose/integration/*.yml)
INTEGRATION_TESTS := $(patsubst tests/compose/integration/%.yml,test-integration-%,$(INTEGRATION_COMPOSE_FILES))

# Integration tests - pattern rule for dynamic discovery
# Any tests/compose/integration/*.yml file automatically becomes test-integration-* target
test-integration-%:
	@echo "🧪 Running $* integration tests..."
	@docker compose -p $(TEST_PROJECT)_$* -f tests/compose/integration/$*.yml down --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_$* -f tests/compose/integration/$*.yml up --build --abort-on-container-exit --exit-code-from integration-test-runner; \
	EXIT_CODE=$$?; \
	FAILED_CONTAINERS=$$(docker compose -p $(TEST_PROJECT)_$* -f tests/compose/integration/$*.yml ps --all -q | xargs -r docker inspect --format '{{.Name}} {{.State.ExitCode}}' | awk '$$2 != 0 && $$2 != 137 && $$2 != 143 {print}'); \
	if [ -n "$$FAILED_CONTAINERS" ]; then \
		echo "$$FAILED_CONTAINERS"; \
		EXIT_CODE=1; \
	fi; \
	docker compose -p $(TEST_PROJECT)_$* -f tests/compose/integration/$*.yml down --remove-orphans; \
	exit $$EXIT_CODE

# The Stage 5 smoke must run on the Docker host: generated compose files bind-mount
# its temporary workspace, so a nested test container cannot see that path.
test-integration-template:
	@$(MAKE) test-integration-template-runner
	@uv run pytest tests/integration/template/test_stage5_mock_smoke.py -k 'not mock_smoke_runs' -v

# TEMPLATE_REF is optional. Without it, the harness reads both production values from
# scripts/system_configs.yaml. Passing it tests a candidate without changing that pin.
test-template-compat:
	@mkdir -p "$(ARTIFACT_DIR)"
	@uv run python tests/integration/template/stage5_mock_smoke.py \
		--workspace-root "$(ARTIFACT_DIR)" \
		--artifact "$(ARTIFACT_DIR)/template-compat-result.json" \
		$(if $(TEMPLATE_REF),--ref "$(TEMPLATE_REF)",)

test-integration-template-runner:
	@docker compose -p $(TEST_PROJECT)_template -f tests/compose/integration/template.yml down --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_template -f tests/compose/integration/template.yml up --build --abort-on-container-exit --exit-code-from integration-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_template -f tests/compose/integration/template.yml down --remove-orphans; \
	exit $$EXIT_CODE

# Run all unit tests locally (no Docker, fast)
# Requires: uv sync (once)
test-unit:
	@uv run bash scripts/test-unit-local.sh

# Run service tests for a specific service using its dedicated compose file
# Usage: make test-service SERVICE=api
test-service:
	@if [ -z "$(SERVICE)" ]; then \
		echo "❌ Error: SERVICE is required (e.g., make test-service SERVICE=api)"; \
		exit 1; \
	fi
	@if [ ! -f "tests/compose/service/$(SERVICE).yml" ]; then \
		echo "❌ Error: Compose file tests/compose/service/$(SERVICE).yml not found"; \
		exit 1; \
	fi
	@echo "🧪 Running $(SERVICE) service tests..."
	@docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml down --remove-orphans 2>/dev/null || true
	@EXIT_CODE=0; \
	if [ "$(SERVICE)" = "worker-manager" ]; then \
		: "The rollout suite deliberately restarts the control-plane containers."; \
		: "Run the pytest container separately from the restarted control plane."; \
		docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml build; \
		EXIT_CODE=$$?; \
		if [ "$$EXIT_CODE" -eq 0 ]; then \
			docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml up -d --wait worker-manager worker-broker; \
			EXIT_CODE=$$?; \
		fi; \
		if [ "$$EXIT_CODE" -eq 0 ]; then \
			docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml run --rm --no-deps $(SERVICE)-test-runner; \
			EXIT_CODE=$$?; \
		fi; \
	else \
		docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml up --build --abort-on-container-exit --exit-code-from $(SERVICE)-test-runner; \
		EXIT_CODE=$$?; \
	fi; \
	FAILED_CONTAINERS=$$(docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml ps --all -q | xargs -r docker inspect --format '{{.Name}} {{.State.ExitCode}}' | awk '$$2 != 0 && $$2 != 137 && $$2 != 143 {print}'); \
	if [ -n "$$FAILED_CONTAINERS" ]; then \
		echo "$$FAILED_CONTAINERS"; \
		EXIT_CODE=1; \
	fi; \
	docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f tests/compose/service/$(SERVICE).yml down --remove-orphans; \
	exit $$EXIT_CODE

# Run all integration tests (auto-discovered from tests/compose/integration/*.yml)
test-integration: $(INTEGRATION_TESTS)
	@echo "✅ All integration tests completed"



LIVE_OFFLINE_IGNORE_FLAGS = \
	--ignore=tests/live/test_api_crud.py \
	--ignore=tests/live/test_bot_access_revocation.py \
	--ignore=tests/live/test_capability_cleanup_redis.py \
	--ignore=tests/live/test_ci_prompt.py \
	--ignore=tests/live/test_deploy_infra.py \
	--ignore=tests/live/test_full_pipeline.py \
	--ignore=tests/live/test_sprint_dod.py \
	--ignore=tests/live/test_health.py \
	--ignore=tests/live/test_parallel_engineering.py \
	--ignore=tests/live/test_pipeline_engineering.py \
	--ignore=tests/live/test_pipeline_scaffold.py \
	--ignore=tests/live/test_scaffold.py \
	--ignore=tests/live/test_scaffold_result.py \
	--ignore=tests/live/test_streams.py \
	--ignore=tests/live/test_supervisor.py

# Offline live regressions: no running stack or external Redis required.
N ?= ""
test-live:
ifeq ($(N),"")
	@echo "Running offline live regressions..."
	@uv run pytest tests/live/ -v --tb=short $(LIVE_OFFLINE_IGNORE_FLAGS)
else
	@echo "Running live test: $(N)..."
	@uv run pytest tests/live/test_$(N).py -v --tb=short
endif

# Pipeline tests: scaffold → engineering → deploy (real GitHub, real queues)
# Set LIVE_NO_CLEANUP=1 to leave owned resources in place after a failed/timed-out
# run for live debugging (manifest kept for `make test-live-clean`). See tests/live/README.md.
test-live-smoke:
	@echo "Running scaffold pipeline test (~1-2 min)..."
	@uv run pytest tests/live/test_pipeline_scaffold.py -v --tb=long -x -s

test-live-engineering:
	@echo "Running engineering pipeline test (~3-5 min)..."
	@uv run pytest tests/live/test_pipeline_engineering.py -v --tb=long -x -s

test-live-mega-noop:
	@echo "Running mega-noop: TestFullPipeline only (no LLM)..."
	@uv run pytest tests/live/test_full_pipeline.py::TestFullPipeline -v --tb=long -x -s

# Temporary compatibility alias. Its exact target is mega-noop, never the whole file.
test-live-mega: test-live-mega-noop

test-live-mega-llm:
	@echo "Running mega-llm: TestFullPipelineLLM only (one selected developer/QA pair)..."
	@uv run pytest tests/live/test_full_pipeline.py::TestFullPipelineLLM -v --tb=long -x -s

# Four paid stand cells: Claude/Codex developer × Claude/Codex QA.
test-live-matrix:
	@$(MAKE) --no-print-directory stand-run SUITE=matrix

# Legacy aggregate, not a named suite: scaffold + engineering + both full-pipeline classes.
test-live-pipeline:
	@echo "Running legacy aggregate: scaffold, engineering, then both full-pipeline classes..."
	@uv run pytest tests/live/test_pipeline_scaffold.py tests/live/test_pipeline_engineering.py tests/live/test_full_pipeline.py -v --tb=long -x -s


# === Stand ===

# Everything a live run needs before it is worth starting: the contour, the two
# subscriptions, disk, docker. A stand idles between runs, and an idle session is
# the one that goes stale — this is where that is found, not eight minutes into a
# mega run.
stand-preflight:
	@set -a; . ./.env; set +a; \
	uv run python -m scripts.stand_preflight

# One entry point for every e2e on the stand. SUITE is a named suite — mega-noop, mega-llm,
# matrix — or any pytest target, so a new scenario needs no new plumbing.
#
#   make stand-run SUITE=mega-noop
#   make stand-run SUITE=mega-llm WORKER=codex QA=claude
#   make stand-run SUITE=matrix
#   make stand-run SUITE=tests/live/test_api_crud.py
#
# A mega takes ten minutes and the matrix an hour — longer than an SSH session
# reliably lives — so run it detached and read the log it names:
#
#   setsid nohup make stand-run SUITE=matrix > /dev/null 2>&1 &
#   tail -f ~/e2e-runs/latest/run.log
SUITE ?= mega-noop
WORKER ?= claude
QA ?= codex
stand-run:
	@set -a; . ./.env; set +a; \
	uv run python -m scripts.stand_run --suite "$(SUITE)" --worker "$(WORKER)" --qa "$(QA)" $(ARGS)

# Kept as the short name for the canonical noop mega.
stand-e2e:
	@$(MAKE) stand-run SUITE=mega-noop

# Sweep this contour and no other.
stand-clean:
	@LIVE_CONTOUR=stand uv run python -m scripts.clean_live_tests

# Cleanup DB and artifacts left by live tests
test-live-clean:
	@echo "🧹 Running comprehensive live test cleanup (DB, GitHub, Workers, Workspaces, Servers)..."
	@uv run python -m scripts.clean_live_tests

# Destroys production and rebuilds it from the deployed revision. Ordinary
# cleanup is `test-live-clean`; this is for when a live run on production has
# left residue across contours and rebuilding is cheaper than picking it apart.
# ARGS is mandatory in practice: without --confirm the script refuses.
#   make danger-prod-reset ARGS="--dry-run"
#   make danger-prod-reset ARGS="--confirm DANGEROUS-PROD-CLEANUP-MEGA-SUPER-PUPER \
#       --allow-telegram-id 625038902 --ssh-key ~/.ssh/codegen_server_ed25519"
danger-prod-reset:
	@uv run python scripts/danger_prod_reset.py $(ARGS)


# E2E Scaffold Test: runs against running `make up` stack
# Creates GitHub repo, publishes CreateWorkerCommand with ScaffoldConfig,
# verifies scaffold files pushed to GitHub, cleans up repo + worker container
test-e2e-scaffold:
	@echo "🧪 Running E2E scaffold test against running stack..."
	@docker compose exec -T langgraph python < scripts/e2e_scaffold_test.py; \
	EXIT_CODE=$$?; \
	echo "🧹 Cleaning up scaffold test containers..."; \
	docker ps -a --filter "name=dev-scaffold-e2e-" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true; \
	exit $$EXIT_CODE



# Cleanup test containers and volumes (all test projects)
test-clean:
	@echo "🧹 Cleaning up test containers and volumes..."
	@for yml in tests/compose/integration/*.yml tests/compose/service/*.yml; do \
		name=$$(basename $$yml .yml); \
		docker compose -p $(TEST_PROJECT)_$$name -f $$yml down -v --remove-orphans 2>/dev/null || true; \
	done
	@echo "✅ Test cleanup complete"


# === Database ===

migrate:
	$(DOCKER_COMPOSE) exec api alembic upgrade head

# Run migrations with correct user to avoid permission issues on generated files
makemigrations:
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) run --rm --user $$(id -u):$$(id -g) api alembic revision --autogenerate -m "$(MSG)"

# === Nuclear Option ===

nuke: BUILD_OPTS=
nuke: .nuke-common

nuke-hard: BUILD_OPTS=--no-cache
nuke-hard: .nuke-hard-prune .nuke-common

.nuke-hard-prune:
	@echo "🧹 Cleaning build cache..."
	@docker builder prune -f

.nuke-common:
	@echo "🔥 Nuking everything (Build mode: $(if $(filter --no-cache,$(BUILD_OPTS)),hard reset,smart incremental))..."
	@echo "💾 Saving server SSH keys before DB wipe..."
	@bash infra/scripts/dump-server-keys.sh || true
	@echo "🧹 Cleaning up stale worker containers..."
	@docker ps -a --filter "name=worker-" --format "{{.Names}}" | grep -v "codegen_orchestrator" | xargs -r docker rm -f 2>/dev/null || true
	@echo "🧹 Cleaning up worker images..."
	@docker images --filter "reference=worker*" -q | xargs -r docker rmi -f 2>/dev/null || true
	$(DOCKER_COMPOSE) down --remove-orphans
	@echo "🧹 Removing volumes (preserving caddy-data for TLS certificates)..."
	@for vol in db_data redis_data caddy-config registry-data; do \
		docker volume rm codegen_orchestrator_$$vol 2>/dev/null || true; \
	done
	$(DOCKER_COMPOSE) --profile build build $(BUILD_OPTS)
	@echo "🔨 Checking worker base images..."
	@$(MAKE) ensure-worker-images
	@echo "🗄️  Starting DB + API only (seed before scheduler to avoid reprovisioning)..."
	$(DOCKER_COMPOSE) up -d db redis api
	@echo "⏳ Waiting for API to be healthy..."
	@timeout=60; \
	while ! curl -s "http://localhost:8000/health" > /dev/null; do \
		if [ $$timeout -le 0 ]; then echo "❌ API failed to start"; exit 1; fi; \
		echo "  Still waiting... ($$timeout s)"; \
		sleep 2; \
		timeout=$$((timeout-2)); \
	done
	$(DOCKER_COMPOSE) exec api alembic upgrade head
	@$(MAKE) seed
	@echo "🚀 Starting remaining services..."
	$(DOCKER_COMPOSE) up -d
	@echo "✅ Fresh environment ready!"

# === Seeding ===

# Every route under /api needs a caller, and seeding is an internal one: the key
# comes from .env, which this Makefile exports. Creating the admin user needs it
# twice over — `is_admin` is a field only an internal caller may set.
seed:
	@echo "🌱 Seeding database..."
	@if [ -z "$$INTERNAL_API_KEY" ]; then \
		echo "  ❌ INTERNAL_API_KEY is not set (expected in .env)"; \
		exit 1; \
	fi
	@if [ -n "$$TIME4VPS_LOGIN" ] && [ -n "$$TIME4VPS_PASSWORD" ]; then \
		curl -fsS -X POST "http://localhost:8000/api/api-keys/" \
			-H "Content-Type: application/json" \
			-H "X-Internal-Key: $$INTERNAL_API_KEY" \
			-d "{\"service\": \"time4vps\", \"type\": \"credentials\", \"value\": {\"username\": \"$$TIME4VPS_LOGIN\", \"password\": \"$$TIME4VPS_PASSWORD\"}}" > /dev/null && \
		echo "  ✅ Time4VPS credentials added"; \
	else \
		echo "  ⚠️  TIME4VPS_LOGIN/PASSWORD not set, skipping"; \
	fi
	@if [ -n "$$TELEGRAM_ID_ADMIN" ]; then \
		status=$$(curl -s -o /dev/null -w "%{http_code}" \
			-H "X-Internal-Key: $$INTERNAL_API_KEY" \
			"http://localhost:8000/api/users/by-telegram/$$TELEGRAM_ID_ADMIN"); \
		if [ "$$status" = "200" ]; then \
			echo "  ⏭️  Admin user ($$TELEGRAM_ID_ADMIN) already exists, skipping"; \
		else \
			curl -fsS -X POST "http://localhost:8000/api/users/" \
				-H "Content-Type: application/json" \
				-H "X-Internal-Key: $$INTERNAL_API_KEY" \
				-d "{\"telegram_id\": $$TELEGRAM_ID_ADMIN, \"username\": \"admin\", \"first_name\": \"Admin\", \"is_admin\": true}" > /dev/null && \
			echo "  ✅ Admin user ($$TELEGRAM_ID_ADMIN) created"; \
		fi; \
	else \
		echo "  ⚠️  TELEGRAM_ID_ADMIN not set, skipping user creation"; \
	fi
	@echo "🖥️  Restoring servers from dump..."
	@bash infra/scripts/restore-server-keys.sh || true
	@echo "🤖 Seeding agent configurations..."
	@$(DOCKER_COMPOSE) exec api python /app/scripts/seed_agent_configs.py \
		--api-base-url http://localhost:8000 \
		--configs-path /app/scripts/agent_configs.yaml || echo "  ⚠️  Agent config seeding failed (API may not be ready)"
	@echo "⚙️  Seeding system configurations..."
	@$(DOCKER_COMPOSE) exec api python /app/scripts/seed_system_configs.py \
		--api-base-url http://localhost:8000 \
		--configs-path /app/scripts/system_configs.yaml || echo "  ⚠️  System config seeding failed (API may not be ready)"
