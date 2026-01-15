.PHONY: lint format test test-unit test-integration test-all test-clean \
	test-api test-api-unit test-api-integration \
	test-langgraph test-langgraph-unit test-langgraph-integration \
	test-scheduler test-scheduler-unit test-scheduler-integration \
	test-telegram test-telegram-unit \
	test-workers-spawner test-orchestrator-cli \
	build up down logs help nuke seed migrate makemigrations shell \
	setup-hooks lock-deps cleanup-agents

# Load .env file
-include .env
export

DOCKER_COMPOSE ?= docker compose
COMPOSE_ENV := HOST_UID=$$(id -u) HOST_GID=$$(id -g)
DOCKER_COMPOSE_TOOLS := $(COMPOSE_ENV) $(DOCKER_COMPOSE) -f docker-compose.tools.yml
TOOLING := $(DOCKER_COMPOSE_TOOLS) run --rm tooling
TOOLING_NON_INT := $(DOCKER_COMPOSE_TOOLS) run --rm -T tooling

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
	@echo "  make test-integration     - Run all integration tests"
	@echo "  make test-all             - Run ALL tests"
	@echo "  make test-legacy          - Run legacy tests (quarantined)"
	@echo "  make test-api-unit        - Run API unit tests"
	@echo "  make test-api-service     - Run API service tests"
	@echo "  make test-clean           - Cleanup test containers"
	@echo ""
	@echo "Git Hooks:"
	@echo "  make setup-hooks  - Install git hooks (format check, tests)"
	@echo ""
	@echo "  make migrate     - Run database migrations"
	@echo "  make makemigrations MSG='...' - Create new migration"
	@echo ""
	@echo "  make shell       - Open shell in tooling container"
	@echo "  make nuke           - Full reset (smart build): remove volumes, incremental rebuild"
	@echo "  make nuke-hard      - Full reset (hard build): remove volumes, NO-CACHE rebuild"
	@echo "  make seed           - Seed database with API keys from env"
	@echo "  make lock-deps      - Regenerate all requirements.lock files"
	@echo "  make cleanup-agents - Remove all agent-* containers"

# === Dependency Lock Files ===

TOOLING_UV := $(DOCKER_COMPOSE_TOOLS) run --rm -e XDG_CACHE_HOME=/workspace/.cache tooling

lock-deps:
	@echo "🔒 Generating requirements.lock files with uv..."
	$(TOOLING_UV) uv pip compile services/langgraph/pyproject.toml -o services/langgraph/requirements.lock
	$(TOOLING_UV) uv pip compile services/api/pyproject.toml -o services/api/requirements.lock
	$(TOOLING_UV) uv pip compile services/scheduler/pyproject.toml -o services/scheduler/requirements.lock
	$(TOOLING_UV) uv pip compile services/telegram_bot/pyproject.toml -o services/telegram_bot/requirements.lock
	$(TOOLING_UV) uv pip compile services/workers-spawner/pyproject.toml -o services/workers-spawner/requirements.lock
	$(TOOLING_UV) uv pip compile services/infrastructure-worker/pyproject.toml -o services/infrastructure-worker/requirements.lock
	@echo "✅ All lock files updated!"

# === Docker ===

up:
	$(DOCKER_COMPOSE) up -d 

down:
	$(DOCKER_COMPOSE) down 

logs:
	$(DOCKER_COMPOSE) logs -f

build:
	$(DOCKER_COMPOSE) --profile build build

# Cleanup orphaned agent containers (manual cleanup)
cleanup-agents:
	@echo "🧹 Cleaning up agent containers..."
	@docker ps -a --filter "name=agent-" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true
	@echo "✅ Agent containers cleaned up"

# === Quality ===

lint:
	@$(DOCKER_COMPOSE_TOOLS) run --rm tooling ruff check .

format:
	@$(DOCKER_COMPOSE_TOOLS) run --rm tooling sh -c "ruff format $(if $(FILES),$(FILES),.) && ruff check --fix $(if $(FILES),$(FILES),.)"

# === Git Hooks ===

setup-hooks:
	@echo "🔧 Installing git hooks..."
	@chmod +x .githooks/pre-commit .githooks/pre-push scripts/setup-hooks.sh
	@bash scripts/setup-hooks.sh


# === Testing ===

# Dynamic discovery of integration test compose files
INTEGRATION_COMPOSE_FILES := $(wildcard docker/test/integration/*.yml)
INTEGRATION_TESTS := $(patsubst docker/test/integration/%.yml,test-integration-%,$(INTEGRATION_COMPOSE_FILES))

# Individual service unit tests (fast, no external deps)
test-api-unit:
	@echo "🧪 Running API unit tests (inside container)..."
	@if [ -d "services/api/tests/unit" ] && [ "$$(ls -A services/api/tests/unit)" ]; then \
		docker compose -p $(TEST_PROJECT)_api -f docker/test/service/api.yml run --rm --no-deps api-test-runner pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/api/tests/unit"; \
	fi

test-api-service:
	@echo "🧪 Running API service tests..."
	@docker compose -p $(TEST_PROJECT)_api -f docker/test/service/api.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_api -f docker/test/service/api.yml up --build --abort-on-container-exit --exit-code-from api-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_api -f docker/test/service/api.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

test-langgraph-unit:
	@echo "🧪 Running LangGraph unit tests..."
	@if [ -d "services/langgraph/tests/unit" ] && [ "$$(ls -A services/langgraph/tests/unit)" ]; then \
		docker compose -p $(TEST_PROJECT)_langgraph -f docker/test/service/langgraph.yml run --rm --no-deps langgraph-test-runner pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/langgraph/tests/unit"; \
	fi

test-langgraph-service:
	@echo "🧪 Running LangGraph service tests..."
	@docker compose -p $(TEST_PROJECT)_langgraph -f docker/test/service/langgraph.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_langgraph -f docker/test/service/langgraph.yml up --build --abort-on-container-exit --exit-code-from langgraph-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_langgraph -f docker/test/service/langgraph.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

test-scheduler-unit:
	@echo "🧪 Running Scheduler unit tests..."
	@if [ -d "services/scheduler/tests/unit" ] && [ "$$(ls -A services/scheduler/tests/unit)" ]; then \
		docker compose -p $(TEST_PROJECT)_scheduler -f docker/test/service/scheduler.yml run --rm --no-deps scheduler-test-runner pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/scheduler/tests/unit"; \
	fi

test-telegram-unit:
	@echo "🧪 Running Telegram bot unit tests..."
	@if [ -d "services/telegram_bot/tests/unit" ] && [ "$$(ls -A services/telegram_bot/tests/unit)" ]; then \
		docker build -f services/telegram_bot/Dockerfile.test -t telegram-bot-test .; \
		docker run --rm telegram-bot-test pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/telegram_bot/tests/unit"; \
	fi

# Integration tests - pattern rule for dynamic discovery
# Any docker/test/integration/*.yml file automatically becomes test-integration-* target
test-integration-%:
	@echo "🧪 Running $* integration tests..."
	@docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml up --build --abort-on-container-exit --exit-code-from integration-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_$* -f docker/test/integration/$*.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

# Note: Legacy aggregate targets (test-api, test-langgraph, etc.) were removed.
# Use test-{service}-unit and test-{service}-service instead.

test-scheduler-service:
	@echo "🧪 Running Scheduler Service tests..."
	@$(DOCKER_COMPOSE) -f docker/test/service/scheduler.yml -p $(TEST_PROJECT)_scheduler build
	@$(DOCKER_COMPOSE) -f docker/test/service/scheduler.yml -p $(TEST_PROJECT)_scheduler up -d db redis api
	@$(DOCKER_COMPOSE) -f docker/test/service/scheduler.yml -p $(TEST_PROJECT)_scheduler run --rm scheduler-test-runner
	@$(DOCKER_COMPOSE) -f docker/test/service/scheduler.yml -p $(TEST_PROJECT)_scheduler down -v

test-orchestrator-cli-unit:
	@echo "🧪 Running Orchestrator CLI unit tests..."
	@if [ -d "shared/orchestrator-cli/tests/unit" ] && [ "$$(ls -A shared/orchestrator-cli/tests/unit)" ]; then \
		docker build -f shared/orchestrator-cli/Dockerfile.test -t orchestrator-cli-test .; \
		docker run --rm orchestrator-cli-test pytest shared/orchestrator-cli/tests/ -v; \
	else \
		echo "⚠️  No unit tests found in shared/orchestrator-cli/tests/unit"; \
	fi

test-worker-wrapper-unit:
	@echo "🧪 Running Worker Wrapper unit tests..."
	@if [ -d "packages/worker-wrapper/tests/unit" ] && [ "$$(ls -A packages/worker-wrapper/tests/unit)" ]; then \
		docker build -f packages/worker-wrapper/Dockerfile.test -t worker-wrapper-test .; \
		docker run --rm worker-wrapper-test pytest packages/worker-wrapper/tests/ -v; \
	else \
		echo "⚠️  No unit tests found in packages/worker-wrapper/tests/unit"; \
	fi

test-infra-service:
	@echo "🧪 Running Infra Service tests..."
	@docker compose -p $(TEST_PROJECT)_infra -f docker/test/service/infra.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_infra -f docker/test/service/infra.yml up --build --abort-on-container-exit --exit-code-from infra-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_infra -f docker/test/service/infra.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

test-worker-manager-unit:
	@echo "🧪 Running Worker Manager unit tests..."
	@if [ -d "services/worker-manager/tests/unit" ] && [ "$$(ls -A services/worker-manager/tests/unit)" ]; then \
		docker build -f services/worker-manager/Dockerfile.test -t worker-manager-test .; \
		docker run --rm worker-manager-test pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/worker-manager/tests/unit"; \
	fi

test-worker-manager-service:
	@echo "🧪 Running Worker Manager service tests..."
	@docker compose -p $(TEST_PROJECT)_worker_manager -f docker/test/service/worker-manager.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_worker_manager -f docker/test/service/worker-manager.yml up --build --abort-on-container-exit --exit-code-from worker-manager-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_worker_manager -f docker/test/service/worker-manager.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

test-scaffolder-unit:
	@echo "🧪 Running Scaffolder unit tests..."
	@if [ -d "services/scaffolder/tests/unit" ] && [ "$$(ls -A services/scaffolder/tests/unit)" ]; then \
		docker compose -p $(TEST_PROJECT)_scaffolder -f docker/test/service/scaffolder.yml run --rm --no-deps scaffolder-test-runner pytest tests/unit/ -v; \
	else \
		echo "⚠️  No unit tests found in services/scaffolder/tests/unit"; \
	fi

test-scaffolder-service:
	@echo "🧪 Running Scaffolder service tests..."
	@docker compose -p $(TEST_PROJECT)_scaffolder -f docker/test/service/scaffolder.yml down -v --remove-orphans 2>/dev/null || true
	@docker compose -p $(TEST_PROJECT)_scaffolder -f docker/test/service/scaffolder.yml up --build --abort-on-container-exit --exit-code-from scaffolder-test-runner; \
	EXIT_CODE=$$?; \
	docker compose -p $(TEST_PROJECT)_scaffolder -f docker/test/service/scaffolder.yml down -v --remove-orphans; \
	exit $$EXIT_CODE

test-shared-unit:
	@echo "🧪 Running Shared unit tests..."
	@if [ -d "shared/tests" ] && [ "$$(ls -A shared/tests)" ]; then \
		docker compose -p $(TEST_PROJECT)_api -f docker/test/service/api.yml run --rm --no-deps -e PYTHONPATH=/app api-test-runner pytest shared/tests/ -v; \
	else \
		echo "⚠️  No unit tests found in shared/tests"; \
	fi

# Run all unit tests (fast)
test-unit: test-api-unit test-langgraph-unit test-scheduler-unit test-telegram-unit test-worker-manager-unit test-orchestrator-cli-unit test-worker-wrapper-unit test-scaffolder-unit test-shared-unit

# Run all integration tests (auto-discovered from docker/test/integration/*.yml)
test-integration: $(INTEGRATION_TESTS)
	@echo "✅ All integration tests completed"

# Run all service tests
test-service: test-api-service test-langgraph-service test-scaffolder-service

# Run ALL tests
test-all: test-unit test-service test-integration

# Legacy test command (runs quarantined tests)
test-legacy:
	@echo "🧟 Running Legacy tests..."
	@$(TOOLING_NON_INT) pytest services/api/tests_legacy \
		services/langgraph/tests_legacy \
		services/scheduler/tests_legacy \
		services/telegram_bot/tests_legacy \
		services/workers-spawner/tests_legacy \
		shared/redis/tests_legacy \
		shared/logging/tests_legacy \
		--ignore=services/api/tests_legacy/integration \
		-v

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

# Run migrations with correct user to avoid permission issues on generated files
makemigrations:
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) run --rm --user $$(id -u):$$(id -g) api alembic revision --autogenerate -m "$(MSG)"

# === Development ===

shell:
	$(TOOLING) bash

# === Nuclear Option ===

nuke: BUILD_OPTS=
nuke: .nuke-common

nuke-hard: BUILD_OPTS=--no-cache
nuke-hard: .nuke-common

.nuke-common:
	@echo "🔥 Nuking everything (Build mode: $(if $(filter --no-cache,$(BUILD_OPTS)),hard reset,smart incremental))..."
	$(DOCKER_COMPOSE) down -v
	$(DOCKER_COMPOSE) --profile build build $(BUILD_OPTS)
	$(DOCKER_COMPOSE) up -d
	@echo "⏳ Waiting for DB to be healthy..."
	@timeout=60; \
	while ! curl -s "http://localhost:8000/health" > /dev/null; do \
		if [ $$timeout -le 0 ]; then echo "❌ API failed to start"; exit 1; fi; \
		echo "  Still waiting... ($$timeout s)"; \
		sleep 2; \
		timeout=$$((timeout-2)); \
	done
	$(DOCKER_COMPOSE) exec api alembic upgrade head
	@$(MAKE) seed
	@echo "✅ Fresh environment ready!"

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
	@echo "🤖 Seeding agent configurations..."
	@$(DOCKER_COMPOSE) exec api python /app/scripts/seed_agent_configs.py \
		--api-base-url http://localhost:8000 \
		--configs-path /app/scripts/agent_configs.yaml \
		--cli-configs-path /app/scripts/cli_agent_configs.yaml || echo "  ⚠️  Agent config seeding failed (API may not be ready)"
