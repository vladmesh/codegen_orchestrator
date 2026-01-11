# Testing Strategy

Стратегия тестирования для новой архитектуры codegen_orchestrator.

## 1. Philosophy

**TDD от контрактов**: У нас зафиксированы контракты (DTO) между сервисами. Тесты проверяют что сервисы соблюдают эти контракты.

**Три уровня:**
```
┌─────────────────────────────────────────────────────────────┐
│  E2E (nightly)                                              │
│  Real GitHub, real containers, full pipeline                │
├─────────────────────────────────────────────────────────────┤
│  Integration (on every PR)                                  │
│  Service + DB + Redis, mock LLM/GitHub                      │
├─────────────────────────────────────────────────────────────┤
│  Unit (on every PR, fast)                                   │
│  Pure functions, no IO                                      │
└─────────────────────────────────────────────────────────────┘
```

**Принципы:**
- Все тесты в контейнерах (изоляция зависимостей)
- Unit: `network_mode: none` (гарантия отсутствия IO)
- Integration: real DB + Redis, mock external APIs
- E2E: real everything, nightly only
- Тесты рядом с кодом (`services/{name}/tests/`)
- Prod images не содержат тесты (multi-stage / .dockerignore)

## 2. Test Levels

### 2.1 Unit Tests

**Что тестируем:**
- Чистые функции без side effects
- Валидация входных данных
- Трансформации данных
- Бизнес-логика в изоляции

**Характеристики:**
- Без network, DB, Redis, файловой системы
- Mocks для всех зависимостей
- < 1 сек на тест
- Запуск: `make test-{service}-unit`

### 2.2 Integration Tests

**Что тестируем:**
- Сервис корректно читает/пишет в DB
- Сервис корректно читает/пишет в Redis queues
- Contract validation (DTO парсится/сериализуется)
- Полный flow внутри одного сервиса

**Характеристики:**
- Real DB (PostgreSQL) + Real Redis
- Mock external APIs (GitHub, LLM, Telegram)
- < 30 сек на тест
- Запуск: `make test-{service}-integration`

### 2.3 E2E Tests

**Что тестируем:**
- Полный pipeline от Telegram до deployed URL
- Реальное взаимодействие между сервисами
- Реальный GitHub (test org)

**Характеристики:**
- Все сервисы запущены
- Real GitHub API (org: `codegen-orchestrator-test`)
- Nightly only (не блокирует PR)
- Cleanup после каждого теста
- Запуск: `make test-e2e`

## 3. Per-Service Test Plan

### 3.1 API Service

| Level | What to Test |
|-------|--------------|
| **Unit** | DTO validation, business logic helpers |
| **Integration** | CRUD operations, DB queries, API endpoints |

**Integration scenarios:**
- [ ] `POST /projects` создаёт project в DB
- [ ] `GET /projects/{id}` возвращает ProjectDTO
- [ ] `POST /tasks` создаёт task, публикует в queue
- [ ] `GET /tasks/{id}` возвращает TaskDTO со статусом
- [ ] Health endpoint отвечает когда DB доступна
- [ ] Health endpoint fails когда DB недоступна

### 3.2 Telegram Bot

| Level | What to Test |
|-------|--------------|
| **Unit** | Message parsing, command routing |
| **Integration** | Session management (Redis), Worker lifecycle |

**Integration scenarios:**
- [ ] Новый user → reject (not in whitelist)
- [ ] Known user, no session → create PO worker via `worker:commands`
- [ ] Known user, active session → reuse worker
- [ ] Known user, dead worker → create new worker
- [ ] Quick command `/projects` → API call, response
- [ ] User message → publish to `worker:po:{user_id}:input`
- [ ] Worker response → read from `worker:po:{user_id}:output`, send to user

**Mocks:**
- Telegram API (aiogram test client)
- API service (httpx mock)
- Worker responses (Redis mock producer)

### 3.3 LangGraph Service

| Level | What to Test |
|-------|--------------|
| **Unit** | Node logic, state transformations |
| **Integration** | Subgraph execution, queue processing |

**Integration scenarios (Engineering Subgraph):**
- [ ] Consume `EngineeringTaskMessage` from `engineering:queue`
- [ ] ScaffolderNode → publish to `scaffolder:queue`, wait for result
- [ ] DeveloperNode → spawn worker via `worker:commands`, get result
- [ ] TesterNode → run tests, pass/fail decision
- [ ] Success → publish `EngineeringTaskResult` to callback stream
- [ ] Failure → retry logic, eventually fail

**Integration scenarios (DevOps Subgraph):**
- [ ] Consume `DeployTaskMessage` from `deploy:queue`
- [ ] EnvAnalyzerNode → determine deploy strategy
- [ ] SecretResolverNode → resolve secrets (mock vault)
- [ ] DeployerNode → publish to `ansible:deploy:queue`
- [ ] Wait for `deploy:result:{request_id}`
- [ ] Success → publish `DeployTaskResult`

**Mocks:**
- LLM responses (scripted MockLLM)
- Worker responses (Redis)
- API (for state updates)

### 3.4 Scheduler

| Level | What to Test |
|-------|--------------|
| **Unit** | Task scheduling logic |
| **Integration** | Background task execution, API sync |

**Integration scenarios:**
- [ ] `github_sync` → fetch repos from GitHub API, update via internal API
- [ ] `server_sync` → fetch server statuses, update via internal API
- [ ] `health_checker` → ping servers, publish alerts on failure
- [ ] Tasks run on schedule (cron)
- [ ] Tasks don't overlap (locking)

**Mocks:**
- GitHub API
- SSH connections (for health checks)
- Internal API

### 3.5 Scaffolder

| Level | What to Test |
|-------|--------------|
| **Unit** | Template selection logic |
| **Integration** | Copier execution, queue processing |

**Integration scenarios:**
- [ ] Consume `ScaffoldTaskMessage` from `scaffolder:queue`
- [ ] Run copier with correct template + answers
- [ ] Commit result to repo (mock git)
- [ ] Publish `ScaffoldTaskResult` to callback stream
- [ ] Handle copier errors gracefully

**Mocks:**
- Git operations
- GitHub API (repo access)

### 3.6 Worker Manager

| Level | What to Test |
|-------|--------------|
| **Unit** | Image hash calculation, config validation |
| **Integration** | Container lifecycle, Docker API |

**Integration scenarios:**
- [ ] `CreateWorkerCommand` → spawn container with correct env
- [ ] `DeleteWorkerCommand` → stop and remove container
- [ ] Worker status tracked in Redis
- [ ] Docker events → status updates
- [ ] Image caching (reuse existing images)
- [ ] Garbage collection (unused images)

**Mocks:**
- Docker API (or real Docker with cleanup)

### 3.7 Infra Service

| Level | What to Test |
|-------|--------------|
| **Unit** | Playbook selection, inventory generation |
| **Integration** | Ansible execution (mock), queue processing |

**Integration scenarios:**
- [ ] Consume `ProvisionerTaskMessage` from `provisioner:queue`
- [ ] Generate inventory from server config
- [ ] Run ansible-playbook (mock subprocess)
- [ ] Publish result to `provisioner:result:{request_id}`
- [ ] Consume `AnsibleDeployMessage` from `ansible:deploy:queue`
- [ ] Run deploy playbook
- [ ] Publish result to `deploy:result:{request_id}`

**Mocks:**
- Ansible subprocess (capture commands, return mock results)
- SSH connections

### 3.8 Worker Wrapper (Package)

| Level | What to Test |
|-------|--------------|
| **Unit** | Message parsing, agent command building |
| **Integration** | Redis communication, agent lifecycle |

**Integration scenarios:**
- [ ] Read from input queue, parse `WorkerInputMessage`
- [ ] Invoke CLI agent (mock subprocess)
- [ ] Capture stdout, publish `WorkerOutputMessage`
- [ ] Handle agent timeout → kill, return timeout status
- [ ] Handle agent crash → return error status
- [ ] Lifecycle events published to `worker:lifecycle`

**Mocks:**
- CLI agent subprocess

## 4. Cross-Service Tests (E2E)

### 4.1 Project Lifecycle

```
User creates project → PO understands → Engineering builds → Deploy works
```

**Scenario: Create and Deploy Blog**
1. User sends "Создай блог на FastAPI"
2. PO Worker creates project via CLI
3. Engineering task triggered
4. Scaffolder creates initial structure
5. Developer adds features (mock LLM)
6. Tests pass
7. Deploy task triggered
8. Ansible deploys (mock)
9. User receives URL

**Assertions:**
- Project exists in DB with correct status
- GitHub repo created (real, in test org)
- PR created with code
- Deploy completed
- All status transitions correct

### 4.2 Error Recovery

**Scenario: Tests Fail, Developer Retries**
1. Engineering task started
2. Developer writes code
3. Tester fails (mock test failure)
4. Developer retries (up to N times)
5. Eventually succeeds or fails permanently

**Assertions:**
- Task status reflects retry attempts
- Final status is COMPLETED or FAILED
- User notified of progress

### 4.3 Worker Failure

**Scenario: Worker Dies Mid-Task**
1. PO Worker processing user message
2. Worker container killed (simulate crash)
3. User sends another message
4. New worker created
5. Session continues (or starts fresh)

**Assertions:**
- Dead worker detected
- New worker spawned
- User experience graceful

## 5. Mock Strategy

### 5.1 LLM Mock

```python
class MockLLM:
    """Deterministic LLM for testing."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls = []

    async def invoke(self, prompt: str) -> LLMResponse:
        self.calls.append(prompt)

        for pattern, response in self.responses.items():
            if pattern.lower() in prompt.lower():
                return response

        return LLMResponse(content="Default response")
```

### 5.2 GitHub Mock

```python
class MockGitHubClient:
    """Mock GitHub for integration tests."""

    def __init__(self):
        self.repos = {}
        self.prs = {}

    async def create_repo(self, name: str) -> Repo:
        repo = Repo(name=name, url=f"https://github.com/test/{name}")
        self.repos[name] = repo
        return repo

    async def create_pr(self, repo: str, branch: str, title: str) -> PR:
        pr = PR(number=len(self.prs) + 1, title=title)
        self.prs[pr.number] = pr
        return pr
```

### 5.3 Subprocess Mock (for Ansible, CLI agents)

```python
class MockSubprocess:
    """Mock for subprocess calls."""

    def __init__(self, responses: dict[str, tuple[int, str, str]]):
        # command pattern -> (returncode, stdout, stderr)
        self.responses = responses
        self.calls = []

    async def run(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)

        cmd_str = " ".join(cmd)
        for pattern, (code, stdout, stderr) in self.responses.items():
            if pattern in cmd_str:
                return CompletedProcess(cmd, code, stdout, stderr)

        return CompletedProcess(cmd, 0, "", "")
```

## 6. Test Infrastructure

### 6.1 Docker Compose for Tests

```yaml
# docker-compose.test.yml

services:
  db-test:
    image: pgvector/pgvector:pg16
    tmpfs: /var/lib/postgresql/data  # Speed
    healthcheck:
      test: pg_isready -U postgres

  redis-test:
    image: redis:7-alpine
    healthcheck:
      test: redis-cli ping
```

### 6.2 Shared Fixtures

```python
# tests/fixtures/conftest.py

@pytest.fixture
async def db():
    """Fresh database for each test."""
    await run_migrations()
    yield get_db_session()
    await truncate_all_tables()

@pytest.fixture
async def redis():
    """Fresh Redis for each test."""
    client = Redis.from_url(REDIS_TEST_URL)
    yield client
    await client.flushdb()

@pytest.fixture
def mock_llm():
    """Preconfigured LLM mock."""
    return MockLLM(responses=STANDARD_RESPONSES)

@pytest.fixture
def mock_github():
    """GitHub mock with cleanup."""
    client = MockGitHubClient()
    yield client
    # No cleanup needed - in-memory
```

## 7. Makefile Commands

```makefile
# === By Type ===
test-unit:           # All unit tests (fast)
test-integration:    # All integration tests
test-e2e:            # E2E tests (nightly)

# === By Service ===
test-api:            # All API tests
test-api-unit:       # API unit only
test-api-integration: # API integration only

test-langgraph:
test-langgraph-unit:
test-langgraph-integration:

# ... same pattern for all services

# === CI/CD ===
test-ci:             # unit + integration (PR gate)
test-nightly:        # everything including e2e

# === Utilities ===
test-clean:          # Remove test containers/volumes
test-coverage:       # Generate coverage report
```

## 8. CI/CD Integration

### 8.1 PR Checks (< 5 min)

```yaml
# .github/workflows/pr.yml
on: [pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test-ci
```

### 8.2 Nightly E2E (< 30 min)

```yaml
# .github/workflows/nightly.yml
on:
  schedule:
    - cron: '0 3 * * *'  # 3 AM UTC

jobs:
  e2e:
    runs-on: ubuntu-latest
    env:
      GITHUB_TEST_TOKEN: ${{ secrets.GITHUB_TEST_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - run: make test-e2e
```

## 9. Open Questions

- [ ] Нужен ли отдельный уровень "smoke tests" для быстрой проверки критического пути?
- [ ] Как тестировать real SSH connections в Infra Service? Testcontainers с SSH?
- [ ] VCR-style recording для GitHub API responses?
- [ ] Как мокать Docker API в Worker Manager? Testcontainers?
- [ ] Coverage threshold? 80%?

## 10. Next Steps

1. [ ] Создать shared fixtures (`tests/fixtures/`)
2. [ ] Реализовать MockLLM, MockGitHub
3. [ ] Обновить docker-compose.test.yml
4. [ ] Добавить contract validation в integration tests
5. [ ] Настроить GitHub test org
6. [ ] Добавить nightly workflow
