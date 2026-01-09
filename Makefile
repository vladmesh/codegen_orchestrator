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
	@echo "  make test-api-unit        - Run API unit tests"
	@echo "  make test-langgraph-unit  - Run LangGraph unit tests"
	@echo "  make test-clean           - Cleanup test containers"
	@echo ""
	@echo "Git Hooks:"
	@echo "  make setup-hooks  - Install git hooks (format check, tests)"
	@echo ""
	@echo "  make migrate     - Run database migrations"
	@echo "  make makemigrations MSG='...' - Create new migration"
	@echo ""
	@echo "  make shell       - Open shell in tooling container"
	@echo "  make nuke           - Full reset: remove volumes, rebuild, migrate"
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

DOCKER_COMPOSE_TEST := DOCKER_BUILDKIT=1 docker compose -p $(TEST_PROJECT) -f docker-compose.test.yml

# Individual service unit tests (fast, no external deps)
test-api-unit:
	@echo "🧪 Running API unit tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from api-test api-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-langgraph-unit:
	@echo "🧪 Running LangGraph unit tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from langgraph-test langgraph-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-scheduler-unit:
	@echo "🧪 Running Scheduler unit tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from scheduler-test scheduler-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-telegram-unit:
	@echo "🧪 Running Telegram bot unit tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from telegram-bot-test telegram-bot-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

# Individual service integration tests (require infrastructure)
test-api-integration:
	@echo "🧪 Running API integration tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from api-test api-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-langgraph-integration:
	@echo "🧪 Running LangGraph integration tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from langgraph-test langgraph-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-scheduler-integration:
	@echo "🧪 Running Scheduler integration tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from scheduler-test scheduler-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

# All tests for a specific service
test-api:
	@echo "🧪 Running all API tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from api-test api-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-langgraph:
	@echo "🧪 Running all LangGraph tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from langgraph-test langgraph-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-scheduler:
	@echo "🧪 Running all Scheduler tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from scheduler-test scheduler-test db-test redis-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-telegram:
	@echo "🧪 Running all Telegram bot tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from telegram-bot-test telegram-bot-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-workers-spawner:
	@echo "🧪 Running Workers Spawner tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from workers-spawner-test workers-spawner-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

test-orchestrator-cli:
	@echo "🧪 Running Orchestrator CLI tests..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 2>/dev/null || true
	@$(DOCKER_COMPOSE_TEST) up --abort-on-container-exit --exit-code-from orchestrator-cli-test orchestrator-cli-test; \
	EXIT_CODE=$$?; \
	$(DOCKER_COMPOSE_TEST) down -v --remove-orphans; \
	exit $$EXIT_CODE

# Run all unit tests (fast)
test-unit: test-api-unit test-langgraph-unit test-scheduler-unit test-telegram-unit

# Run all integration tests
test-integration: test-api-integration test-langgraph-integration test-scheduler-integration

# Run ALL tests
test-all: test-api test-langgraph test-scheduler test-telegram test-workers-spawner test-orchestrator-cli

# Legacy test command (now runs all tests)
test:
ifdef SERVICE
	@$(MAKE) test-$(SERVICE)
else
	@$(MAKE) test-all
endif

# Cleanup test containers and volumes
test-clean:
	@echo "🧹 Cleaning up test containers and volumes..."
	@$(DOCKER_COMPOSE_TEST) down -v --remove-orphans 


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

nuke:
	@echo "🔥 Nuking everything..."
	$(DOCKER_COMPOSE) down -v
	$(DOCKER_COMPOSE) --profile build build --no-cache
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
