.PHONY: setup lint format typecheck tests infra-start ps worker-start worker-stop worker-clean smoke-probe worker-call down dev-start dev-smoke dev-stop dev-clean prod-start prod-stop migrate makemigrations generate-from-spec validate-specs lint-controllers openapi typescript test-integration log tooling-tests check-deps

-include .env
export

DOCKER_COMPOSE ?= docker compose
COMPOSE_BASE := -f infra/compose.base.yml
COMPOSE_DEV := $(COMPOSE_BASE) -f infra/compose.dev.yml
COMPOSE_LOCAL := $(COMPOSE_DEV) -f infra/compose.local.yml
COMPOSE_PROD := $(COMPOSE_BASE) -f infra/compose.prod.yml
COMPOSE_TEST_INTEGRATION := -f infra/compose.tests.integration.yml
CHECKOUT_UID := $(shell stat -c '%u' .)
CHECKOUT_GID := $(shell stat -c '%g' .)
COMPOSE_ENV_INTEGRATION := COMPOSE_PROJECT_NAME=tests-integration HOST_UID=$(CHECKOUT_UID) HOST_GID=$(CHECKOUT_GID)
COMPOSE_ENV_DEV_SMOKE := COMPOSE_PROJECT_NAME=env_fixture-dev-smoke
INFRA_SERVICES := db redis
SMOKE_RUNNER ?= backend
SMOKE_URL ?= http://backend:8000/health
method ?= POST
body ?=

VENV := .venv/bin
PYTHON := $(VENV)/python

ALEMBIC_CONFIG := services/backend/migrations/alembic.ini
BACKEND_ALEMBIC := PYTHONPATH=. services/backend/.venv/bin/alembic -c $(ALEMBIC_CONFIG)
DOCKER_ALEMBIC := $(DOCKER_COMPOSE) $(COMPOSE_DEV) run --rm --build backend alembic -c $(ALEMBIC_CONFIG)
ifeq ($(SKIP_INFRA_START),1)
ALEMBIC := $(BACKEND_ALEMBIC)
else
ALEMBIC := $(DOCKER_ALEMBIC)
endif


ifeq ($(word 1,$(MAKECMDGOALS)),log)
LOG_SERVICE := $(word 2,$(MAKECMDGOALS))
ifneq ($(LOG_SERVICE),)
$(eval $(LOG_SERVICE):;@:)
endif
endif

ifeq ($(word 1,$(MAKECMDGOALS)),tests)
TEST_TARGET := $(word 2,$(MAKECMDGOALS))
ifneq ($(TEST_TARGET),)
$(eval $(TEST_TARGET):;@:)
endif
endif

setup:
	uv venv --quiet 2>/dev/null || uv venv --clear --quiet
	uv pip install -e .framework/ ruff xenon
	@for svc in services/*/; do \
		[ -f "$$svc/pyproject.toml" ] || continue; \
		echo ">> Setting up $$svc"; \
		(cd "$$svc" && uv sync --frozen); \
	done
	@echo ">> Generating code from specs"
	$(PYTHON) -m framework.generate
	@echo ">> Formatting generated code"
	$(VENV)/ruff format .
	@$(VENV)/ruff check --fix . || { \
		echo ">> Warning: lint auto-fix left issues in the generated project."; \
		echo ">> Setup continues; run 'make lint' to see and fix them."; \
	}
	@git config core.hooksPath .githooks 2>/dev/null || true
	@echo ">> Setup complete!"

lint:
	$(VENV)/ruff format --check .
	$(VENV)/ruff check .
	$(VENV)/xenon --max-absolute B --max-modules A --max-average A --exclude '.framework/*,tests/*,services/*/tests/*' .
	$(PYTHON) -c "from framework.spec.loader import validate_specs_cli; from framework.lib.env import get_repo_root; ok, msg = validate_specs_cli(get_repo_root()); print(msg); exit(0 if ok else 1)"
	$(PYTHON) -m framework.enforce_spec_compliance
	$(PYTHON) -c "from framework.lint.controller_sync import lint_controllers_cli; from framework.lib.env import get_repo_root; ok, msg = lint_controllers_cli(get_repo_root()); print(msg); exit(0 if ok else 1)"
	$(MAKE) check-deps

lint-complexity:
	$(VENV)/xenon --max-absolute B --max-modules A --max-average A --exclude ".framework/*,tests/*,services/*/tests/*" .

check-deps:
	@for svc in services/*/; do \
		[ -f "$$svc/.venv/bin/deptry" ] || continue; \
		echo ">> Checking dependencies: $$(basename $$svc)"; \
		(cd "$$svc" && .venv/bin/deptry src/) || exit 1; \
	done

validate-specs:
	$(PYTHON) -c "from framework.spec.loader import validate_specs_cli; from framework.lib.env import get_repo_root; ok, msg = validate_specs_cli(get_repo_root()); print(msg); exit(0 if ok else 1)"

lint-specs:
	$(PYTHON) -m framework.enforce_spec_compliance

format:
	$(VENV)/ruff format .
	$(VENV)/ruff check --fix .

typecheck:
	@for svc in services/*/; do \
		[ -f "$$svc/.venv/bin/mypy" ] || continue; \
		echo ">> Typechecking $$(basename $$svc)"; \
		PYTHONPATH=. MYPYPATH=. "$$svc/.venv/bin/mypy" "$$svc"; \
	done

tests:
	@target="$(TEST_TARGET)"; \
	if [ "$$target" = "tooling" ]; then $(MAKE) tooling-tests; exit 0; fi; \
	if [ -n "$$target" ] && [ "$$target" != "all" ]; then \
		svc="services/$$target"; \
		[ -d "$$svc/tests" ] || { echo "No tests for $$target"; exit 1; }; \
		echo ">> Testing $$target"; \
		PYTHONPATH=. "$$svc/.venv/bin/pytest" "$$svc/tests/" -q; \
		exit 0; \
	fi; \
	failed=0; \
	for svc in services/*/; do \
		[ -d "$$svc/tests" ] || continue; \
		echo ">> Testing $$(basename $$svc)"; \
		PYTHONPATH=. "$$svc/.venv/bin/pytest" "$$svc/tests/" -q || failed=1; \
	done; \
	$(MAKE) tooling-tests || failed=1; \
	exit $$failed

migrate:
	@if [ "$(SKIP_INFRA_START)" != "1" ]; then $(MAKE) worker-start svc=db; fi
	@if [ "$(SKIP_INFRA_START)" = "1" ] && [ ! -x "services/backend/.venv/bin/alembic" ]; then \
		echo "services/backend/.venv/bin/alembic not found; run uv sync --project services/backend"; \
		exit 1; \
	fi
	$(ALEMBIC) upgrade head

makemigrations:
	@if [ -z "$(name)" ]; then \
		echo "Usage: make makemigrations name=\"<description>\""; \
		exit 1; \
	fi
	@if [ "$(SKIP_INFRA_START)" != "1" ]; then $(MAKE) worker-start svc=db; fi
	@if [ "$(SKIP_INFRA_START)" = "1" ] && [ ! -x "services/backend/.venv/bin/alembic" ]; then \
		echo "services/backend/.venv/bin/alembic not found; run uv sync --project services/backend"; \
		exit 1; \
	fi
	$(ALEMBIC) upgrade head
	$(ALEMBIC) revision --autogenerate -m "$(name)"

test-integration:
	$(COMPOSE_ENV_INTEGRATION) $(DOCKER_COMPOSE) $(COMPOSE_TEST_INTEGRATION) up --build --abort-on-container-exit --exit-code-from integration-tests
	$(COMPOSE_ENV_INTEGRATION) $(DOCKER_COMPOSE) $(COMPOSE_TEST_INTEGRATION) down --volumes --remove-orphans

log:
	@service="$(if $(service),$(service),$(LOG_SERVICE))"; \
	if [ -z "$$service" ]; then \
		echo "Usage: make log <service_name>"; \
		echo "       make log service=\"<service_name>\""; \
		exit 1; \
	fi; \
	echo ">> Showing logs for $$service"; \
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) logs --tail=100 $$service

infra-start:
	@if [ -z "$(strip $(INFRA_SERVICES))" ]; then \
		echo "No infrastructure services selected, skipping"; \
	else \
		$(DOCKER_COMPOSE) $(COMPOSE_DEV) up -d --wait $(INFRA_SERVICES); \
	fi

ps:
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) ps

worker-start:
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) up -d --build --wait $(svc)

worker-stop:
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) down --remove-orphans

worker-clean:
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) down --volumes --remove-orphans

down: worker-stop

smoke-probe:
	@if [ -z "$(SMOKE_RUNNER)" ] || [ -z "$(SMOKE_URL)" ]; then \
		echo "Usage: make smoke-probe SMOKE_RUNNER=<python_service> SMOKE_URL=http://service:port/path"; \
		exit 1; \
	fi
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) run --rm --no-deps $(SMOKE_RUNNER) python -c 'import sys, urllib.request; url = sys.argv[1]; response = urllib.request.urlopen(url, timeout=10); print(f"{url} -> {response.status}"); sys.exit(0 if response.status < 400 else 1)' "$(SMOKE_URL)"

worker-call:
	@if [ -z "$(SMOKE_RUNNER)" ] || [ -z "$(url)" ]; then \
		echo "Usage: make worker-call SMOKE_RUNNER=<python_service> url=http://service:port/path [method=POST] [body='{}']"; \
		exit 1; \
	fi
	$(DOCKER_COMPOSE) $(COMPOSE_DEV) run --rm --no-deps $(SMOKE_RUNNER) python -c 'import sys, urllib.request; url, method, body = sys.argv[1:4]; data = body.encode() if body else None; request = urllib.request.Request(url, data=data, method=method.upper()); request.add_header("Content-Type", "application/json"); response = urllib.request.urlopen(request, timeout=10); print(f"{method.upper()} {url} -> {response.status}"); sys.exit(0 if response.status < 400 else 1)' "$$url" "$$method" "$$body"

dev-start:
	$(DOCKER_COMPOSE) $(COMPOSE_LOCAL) up -d --build --wait $(svc)

dev-smoke:
	@set -e; \
	services="backend tg_bot notifications_worker"; \
	if [ -z "$$services" ]; then \
		echo "No Python services selected, skipping"; \
		exit 0; \
	fi; \
	cleanup() { $(COMPOSE_ENV_DEV_SMOKE) $(DOCKER_COMPOSE) $(COMPOSE_DEV) down --volumes --remove-orphans >/dev/null 2>&1 || true; }; \
	trap cleanup EXIT; \
	for svc in $$services; do \
		echo ">> Smoke testing $$svc in dev compose"; \
		$(COMPOSE_ENV_DEV_SMOKE) $(DOCKER_COMPOSE) $(COMPOSE_DEV) run --rm --build --no-deps "$$svc" python -c 'import sys; print(sys.executable); assert sys.executable.startswith("/app/services/")'; \
	done

dev-stop:
	$(DOCKER_COMPOSE) $(COMPOSE_LOCAL) down --remove-orphans

dev-clean:
	$(DOCKER_COMPOSE) $(COMPOSE_LOCAL) down --volumes --remove-orphans

prod-start:
	$(DOCKER_COMPOSE) $(COMPOSE_PROD) up -d --remove-orphans

prod-stop:
	$(DOCKER_COMPOSE) $(COMPOSE_PROD) down --remove-orphans

tooling-tests:
	@dirs=""; \
	[ -d tests/tooling ] && dirs="$$dirs tests/tooling"; \
	[ -d tests/unit ] && dirs="$$dirs tests/unit"; \
	if [ -z "$$dirs" ]; then \
		echo "No tooling/unit tests found, skipping"; \
	else \
		PYTHONPATH=.framework $(VENV)/pytest -q --cov=framework --cov-report=term-missing --cov-fail-under=70 $$dirs; \
	fi

generate-from-spec:
	$(PYTHON) -m framework.generate

lint-controllers:
	$(PYTHON) -c "from framework.lint.controller_sync import lint_controllers_cli; from framework.lib.env import get_repo_root; ok, msg = lint_controllers_cli(get_repo_root()); print(msg); exit(0 if ok else 1)"

openapi:
	$(PYTHON) -m framework.openapi.generator

typescript:
	$(PYTHON) -m framework.frontend.generator

