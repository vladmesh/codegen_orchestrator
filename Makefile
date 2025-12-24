.PHONY: lint format test build up down logs help

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
	$(TOOLING) pytest -v --cov=src --cov-report=term-missing tests/

# === Database ===

migrate:
	$(DOCKER_COMPOSE) exec api alembic upgrade head

# Run migrations with correct user to avoid permission issues on generated files
makemigrations:
	$(COMPOSE_ENV) $(DOCKER_COMPOSE) run --rm --user $$(id -u):$$(id -g) api alembic revision --autogenerate -m "$(MSG)"

# === Development ===

shell:
	$(TOOLING) bash
