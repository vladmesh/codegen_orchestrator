.PHONY: lint format test-unit test-integration test-e2e-scaffold test-live test-live-clean test-clean \
	build up down stop logs help nuke nuke-hard seed migrate makemigrations init-langfuse-db \
	setup-hooks lock-deps cleanup-agents backlog roadmap status recent-artifacts sync task \
	rebuild-worker-images rebuild-worker-images-hard rebuild \
	check-worker-images .nuke-common .nuke-hard-prune pull-worker-reports

# Load .env file
-include .env
export

DOCKER_COMPOSE ?= docker compose

# Hash of source files baked into worker images.
# Only includes shared submodules actually imported by worker-wrapper and orchestrator-cli,
# plus the packages themselves and worker Dockerfiles.
# Changes to e.g. shared/models/ or shared/schemas/ won't trigger a worker rebuild.
WORKER_SOURCE_HASH = $(shell find \
  shared/__init__.py shared/log_config shared/redis shared/redis_client.py \
  shared/config.py shared/queues.py shared/contracts shared/crypto.py shared/constants.py \
  packages/worker-wrapper packages/orchestrator-cli \
  services/worker-manager/images -type f \
  -not -path '*/__pycache__/*' -not -name '*.pyc' \
  | LC_ALL=C sort | xargs sha256sum 2>/dev/null | sha256sum | cut -c1-16)

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
	@echo "  make cleanup-agents - Remove all agent-* containers"
	@echo ""
	@echo "  make rebuild      - Rebuild everything (services + worker images), restart stack"
	@echo ""
	@echo "Worker Images:"
	@echo "  make rebuild-worker-images      - Rebuild worker base images (common → claude)"
	@echo "  make rebuild-worker-images-hard - Rebuild with --no-cache (when cache is stale)"

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
	@$(MAKE) check-worker-images

# === Full Rebuild (with cache) ===
# Stops stack, kills workers, rebuilds all service + worker images, restarts
rebuild:
	@echo "🔄 Rebuilding everything..."
	@echo "🛑 Stopping stack..."
	$(DOCKER_COMPOSE) down --remove-orphans
	@echo "🔪 Killing worker containers..."
	@docker ps -a --filter "name=worker-" --format "{{.Names}}" | grep -v "codegen_orchestrator" | xargs -r docker rm -f 2>/dev/null || true
	@echo "🧹 Cleaning cached worker:* images..."
	@docker images -q 'worker:*' | xargs -r docker rmi 2>/dev/null || true
	@echo "🔨 Building service images..."
	$(DOCKER_COMPOSE) --profile build build
	@echo "🔨 Building worker base images..."
	@$(MAKE) rebuild-worker-images
	@echo "🚀 Starting stack..."
	$(DOCKER_COMPOSE) up -d
	@echo "✅ Rebuild complete!"

# Cleanup orphaned agent containers (manual cleanup)
cleanup-agents:
	@echo "🧹 Cleaning up agent containers..."
	@docker ps -a --filter "name=agent-" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true
	@echo "✅ Agent containers cleaned up"

# === Worker Base Images ===
# Build the worker image chain: common -> claude/factory
# Use rebuild-worker-images after changing orchestrator-cli or worker-base Dockerfiles

rebuild-worker-images:
	@echo "🔨 Building worker-base-common..."
	docker build --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		-t worker-base-common:latest \
		-f services/worker-manager/images/worker-base-common/Dockerfile .
	@echo "🔨 Building worker-base-claude..."
	docker build -t worker-base-claude:latest \
		-f services/worker-manager/images/worker-base-claude/Dockerfile .
	@echo "🧹 Cleaning cached worker:* images..."
	@docker images -q 'worker:*' | xargs -r docker rmi 2>/dev/null || true
	@echo "✅ Worker images rebuilt!"

# Full rebuild with --no-cache (use when Docker cache is stale)
rebuild-worker-images-hard:
	@echo "🔨 Building worker-base-common (no-cache)..."
	docker build --no-cache --build-arg SOURCE_HASH=$(WORKER_SOURCE_HASH) \
		-t worker-base-common:latest \
		-f services/worker-manager/images/worker-base-common/Dockerfile .
	@echo "🔨 Building worker-base-claude (no-cache)..."
	docker build --no-cache -t worker-base-claude:latest \
		-f services/worker-manager/images/worker-base-claude/Dockerfile .
	@echo "🧹 Cleaning cached worker:* images..."
	@docker images -q 'worker:*' | xargs -r docker rmi 2>/dev/null || true
	@echo "✅ Worker images rebuilt (no-cache)!"

# Check if worker images are stale and rebuild if needed
check-worker-images:
	@CURRENT=$(WORKER_SOURCE_HASH); \
	STORED=$$(docker inspect worker-base-common:latest \
	  --format '{{index .Config.Labels "org.codegen.worker_source_hash"}}' 2>/dev/null || echo "none"); \
	if [ "$$CURRENT" != "$$STORED" ]; then \
	  echo "⚠️  Worker source files changed ($$STORED → $$CURRENT) — rebuilding images..."; \
	  $(MAKE) rebuild-worker-images; \
	else \
	  echo "✅ Worker images up to date (hash: $$CURRENT)"; \
	fi

# === Quality ===

lint:
	@uv run ruff check $(if $(LINT_PATH),$(LINT_PATH),.)

format:
	@uv run ruff format $(if $(FILES),$(FILES),.) && uv run ruff check --fix $(if $(FILES),$(FILES),.)

# === Git Hooks ===

setup-hooks:
	@echo "🔧 Installing git hooks..."
	@chmod +x .githooks/pre-commit .githooks/pre-push scripts/setup-hooks.sh
	@bash scripts/setup-hooks.sh


# === Testing ===

# Integration tests - pattern rule for dynamic discovery
# Any docker/test/integration/*.yml file automatically becomes test-integration-* target
INTEGRATION_COMPOSE_FILES := $(wildcard docker/test/integration/*.yml)
INTEGRATION_TESTS := $(patsubst docker/test/integration/%.yml,test-integration-%,$(INTEGRATION_COMPOSE_FILES))

# Integration tests - pattern rule for dynamic discovery
# Any docker/test/integration/*.yml file automatically becomes test-integration-* target
test-integration-%:
	@echo "🧪 Running $* integration tests..."
	@docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml down --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml up --build --abort-on-container-exit --exit-code-from integration-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml down --remove-orphans; \
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
	@if [ ! -f "docker/test/service/$(SERVICE).yml" ]; then \
		echo "❌ Error: Compose file docker/test/service/$(SERVICE).yml not found"; \
		exit 1; \
	fi
	@echo "🧪 Running $(SERVICE) service tests..."
	@docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f docker/test/service/$(SERVICE).yml down --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f docker/test/service/$(SERVICE).yml up --build --abort-on-container-exit --exit-code-from $(SERVICE)-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_service_$(SERVICE) -f docker/test/service/$(SERVICE).yml down --remove-orphans; \
	exit $$EXIT_CODE

# Run all integration tests (auto-discovered from docker/test/integration/*.yml)
test-integration: $(INTEGRATION_TESTS)
	@echo "✅ All integration tests completed"



# Live tests: run from host against running `make up` stack (no LLM)
N ?= ""
test-live:
ifeq ($(N),"")
	@echo "Running all live tests (excluding pipeline)..."
	@uv run pytest tests/live/ -v --tb=short --ignore=tests/live/test_pipeline_scaffold.py --ignore=tests/live/test_pipeline_engineering.py --ignore=tests/live/test_full_pipeline.py
else
	@echo "Running live test: $(N)..."
	@uv run pytest tests/live/test_$(N).py -v --tb=short
endif

# Pipeline tests: scaffold → engineering → deploy (real GitHub, real queues)
test-live-smoke:
	@echo "Running scaffold pipeline test (~1-2 min)..."
	@uv run pytest tests/live/test_pipeline_scaffold.py -v --tb=long -x -s

test-live-engineering:
	@echo "Running engineering pipeline test (~3-5 min)..."
	@uv run pytest tests/live/test_pipeline_engineering.py -v --tb=long -x -s

test-live-mega:
	@echo "Running MEGA pipeline test (~7-10 min)..."
	@uv run pytest tests/live/test_full_pipeline.py -v --tb=long -x -s

test-live-pipeline:
	@echo "Running ALL pipeline tests sequentially (~15 min)..."
	@uv run pytest tests/live/test_pipeline_scaffold.py tests/live/test_pipeline_engineering.py tests/live/test_full_pipeline.py -v --tb=long -x -s


# Cleanup DB and artifacts left by live tests
test-live-clean:
	@echo "🧹 Running comprehensive live test cleanup (DB, GitHub, Workers, Workspaces, Servers)..."
	@uv run python scripts/clean_live_tests.py


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
	@for yml in docker/test/integration/*.yml docker/test/service/*.yml; do \
		name=$$(basename $$yml .yml); \
		docker compose -p $(TEST_PROJECT)_$$name -f $$yml down -v --remove-orphans 2>/dev/null || true; \
	done
	@echo "✅ Test cleanup complete"


# === Database ===

migrate:
	$(DOCKER_COMPOSE) exec api alembic upgrade head

# Create langfuse database on existing postgres (init script only runs on fresh volumes)
init-langfuse-db:
	$(DOCKER_COMPOSE) exec db psql -U $${POSTGRES_USER:-postgres} -c "SELECT 'CREATE DATABASE langfuse' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec"

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
	@$(MAKE) check-worker-images
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

# === Backlog ===

backlog:
	@uv run python scripts/generate_backlog.py

roadmap:
	@uv run python scripts/generate_roadmap.py

status:
	@uv run python scripts/generate_status.py

pull-worker-reports:
	@uv run python scripts/pull_worker_reports.py

recent-artifacts:
	@uv run python scripts/sync_recent_artifacts.py

sync: backlog roadmap status recent-artifacts

task:
ifndef TITLE
	$(error Usage: make task TITLE="task title" [DESC="description"])
endif
	@PID=$$(curl -sf "http://localhost:8000/api/projects/" | python3 -c "import sys,json; ps=json.load(sys.stdin); print(ps[0]['id'] if ps else '')") && \
	curl -sf -X POST "http://localhost:8000/api/tasks/push" \
		-H "Content-Type: application/json" \
		-d "{\"title\": \"$(TITLE)\", \"description\": \"$(DESC)\", \"project_id\": \"$$PID\"}" \
		| python3 -c "import sys,json; t=json.load(sys.stdin); print(f'Created: {t[\"id\"]} (p={t[\"priority\"]}) {t[\"title\"]}')"

# === Seeding ===

seed:
	@echo "🌱 Seeding database..."
	@if [ -n "$$TIME4VPS_LOGIN" ] && [ -n "$$TIME4VPS_PASSWORD" ]; then \
		curl -fsS -X POST "http://localhost:8000/api/api-keys/" \
			-H "Content-Type: application/json" \
			-d "{\"service\": \"time4vps\", \"type\": \"credentials\", \"value\": {\"username\": \"$$TIME4VPS_LOGIN\", \"password\": \"$$TIME4VPS_PASSWORD\"}}" > /dev/null && \
		echo "  ✅ Time4VPS credentials added"; \
	else \
		echo "  ⚠️  TIME4VPS_LOGIN/PASSWORD not set, skipping"; \
	fi
	@if [ -n "$$TELEGRAM_ID_ADMIN" ]; then \
		status=$$(curl -s -o /dev/null -w "%{http_code}" \
			"http://localhost:8000/api/users/by-telegram/$$TELEGRAM_ID_ADMIN"); \
		if [ "$$status" = "200" ]; then \
			echo "  ⏭️  Admin user ($$TELEGRAM_ID_ADMIN) already exists, skipping"; \
		else \
			curl -fsS -X POST "http://localhost:8000/api/users/" \
				-H "Content-Type: application/json" \
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
