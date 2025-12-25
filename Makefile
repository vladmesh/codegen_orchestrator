.PHONY: lint format test build up down logs help nuke seed

# Load .env file
-include .env
export

DOCKER_COMPOSE ?= docker compose
COMPOSE_ENV := HOST_UID=$$(id -u) HOST_GID=$$(id -g)
TOOLING := $(COMPOSE_ENV) $(DOCKER_COMPOSE) run --rm tooling

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
	@echo "  make test        - Run tests"
	@echo ""
	@echo "  make migrate     - Run database migrations"
	@echo "  make makemigrations MSG='...' - Create new migration"
	@echo ""
	@echo "  make shell       - Open shell in tooling container"
	@echo "  make nuke        - Full reset: remove volumes, rebuild, migrate"
	@echo "  make seed        - Seed database with API keys from env"

# === Docker ===

up:
	$(DOCKER_COMPOSE) up -d

down:
	$(DOCKER_COMPOSE) down

logs:
	$(DOCKER_COMPOSE) logs -f

build:
	$(DOCKER_COMPOSE) build

# === Quality ===

lint:
	$(TOOLING) ruff check .

format:
	$(TOOLING) sh -c "ruff format . && ruff check --fix ."

test:
	$(TOOLING) bash -lc "export HOME=/tmp && pip install -e ./services/api -e ./services/langgraph && pytest -v --cov=src --cov-report=term-missing tests/"

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
	$(DOCKER_COMPOSE) build --no-cache
	$(DOCKER_COMPOSE) up -d
	@echo "⏳ Waiting for DB to be healthy..."
	@sleep 5
	$(DOCKER_COMPOSE) exec api alembic upgrade head
	@$(MAKE) seed
	@echo "✅ Fresh environment ready!"

# === Seeding ===

seed:
	@echo "🌱 Seeding database..."
	@if [ -n "$$TIME4VPS_LOGIN" ] && [ -n "$$TIME4VPS_PASSWORD" ]; then \
		curl -s -X POST "http://localhost:8000/api/api-keys/" \
			-H "Content-Type: application/json" \
			-d "{\"service\": \"time4vps\", \"type\": \"credentials\", \"value\": {\"username\": \"$$TIME4VPS_LOGIN\", \"password\": \"$$TIME4VPS_PASSWORD\"}}" > /dev/null && \
		echo "  ✅ Time4VPS credentials added"; \
	else \
		echo "  ⚠️  TIME4VPS_LOGIN/PASSWORD not set, skipping"; \
	fi
	@if [ -n "$$TELEGRAM_ID_ADMIN" ]; then \
		curl -s -X POST "http://localhost:8000/users/" \
			-H "Content-Type: application/json" \
			-d "{\"telegram_id\": $$TELEGRAM_ID_ADMIN, \"username\": \"admin\", \"first_name\": \"Admin\", \"is_admin\": true}" > /dev/null && \
		echo "  ✅ Admin user ($$TELEGRAM_ID_ADMIN) created"; \
	else \
		echo "  ⚠️  TELEGRAM_ID_ADMIN not set, skipping user creation"; \
	fi
	@echo "🤖 Seeding agent configurations..."
	@$(DOCKER_COMPOSE) exec api python /app/scripts/seed_agent_configs.py --api-url http://localhost:8000 || echo "  ⚠️  Agent config seeding failed (API may not be ready)"


