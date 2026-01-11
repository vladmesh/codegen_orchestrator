# Testing Strategy: Worker Manager

Этот документ детализирует стратегию тестирования сервиса `worker-manager`, делая упор на использование **Real Docker**, тестирование кэширования слоев и безопасную изоляцию тестов.

## 1. Philosophy

*   **Real Docker**: Мы не мокаем Docker Daemon в интеграционных тестах. Мы используем реальный сокет хоста (или dind), чтобы гарантировать корректность работы с контейнерами.
*   **Host Caching**: Мы полагаемся на локальный кэш образов Docker для ускорения тестов и работы. Тесты должны проверять эффективность этого кэширования.
*   **Safe Isolation**: Тесты не должны мешать боевым контейнерам или удалять боевые образы.

---

## 2. Test Isolation Strategy

Чтобы безопасно запускать тесты на машине разработчика или CI, мы используем стратегию **Namespacing & Labelling**.

### 2.1 Image Prefix
В конфигурацию `WorkerManagerSettings` добавляется параметр `IMAGE_PREFIX`.

*   **Production**: `IMAGE_PREFIX="worker"` (образы вида `worker:a1b2c3...`)
*   **Testing**: `IMAGE_PREFIX="worker-test"` (образы вида `worker-test:a1b2c3...`)

Это позволяет командам `docker rmi worker-test:*` (CleanUp) никогда не затрагивать боевые образы, даже если хэш слоев совпадает.

### 2.2 Docker Labels
Все контейнеры и образы, создаваемые в тестах, помечаются лейблами:

*   `com.codegen.environment="test"`
*   `com.codegen.test_session_id="<uuid>"` (для параллельного запуска)

Garbage Collector в тестовом режиме настраивается на удаление **только** ресурсов с этими лейблами.

---

## 3. Test Layers

### 3.1 Unit Tests
*Pure python logic, no Docker interaction.*

*   **Image Hash Calculation**:
    *   Input: `capabilities=["GIT", "DOCKER"]` vs `["DOCKER", "GIT"]`.
    *   Assert: Хэши идентичны.
*   **Config Validation**:
    *   Assert: `create_worker` падает с валидационной ошибкой при неизвестном `agent_type` или запрещенных символах в `allowed_commands`.
*   **Layer Selection**:
    *   Assert: Для `modules=["GIT"]` выбирается правильный набор инструкций для Dockerfile.

### 3.2 Service Tests (Mock Docker)
*Focus on orchestrator logic & Redis state machinery.*

Запускаем сервис с `MockDockerClient`.
*   **State Transitions**:
    *   Action: Send `create` command.
    *   Assert: Redis status `STARTING` -> `RUNNING`.
    *   Assert: MockClient received `run()` with correct Env Vars (`SESSION_ID`, `ALLOWED_COMMANDS`).
*   **Reconciliation Loop**:
    *   Action: Emit mock docker event `die`.
    *   Assert: Redis status updates to `FAILED/STOPPED`.

### 3.3 Integration Tests (Real Docker)
*The Core Test Suite. Requires access to `/var/run/docker.sock`.*

#### Scenario A: Lifecycle
1.  **Spawn**: Отправляем команду на создание воркера.
2.  **Assert**: `docker ps` показывает бегущий контейнер с именем `worker-test-<id>`.
3.  **Assert**: Внутри контейнера установлены правильные Env Vars (через `docker inspect`).
4.  **Terminate**: Отправляем команду `delete`.
5.  **Assert**: Контейнер остановлен и удален.

#### Scenario B: Caching (Hit vs Miss)
Проверяем, что кэширование работает и мы не пересобираем образы зря.

1.  **Cleanup**: Принудительно удаляем `worker-test:<hash>` (если есть).
2.  **First Run (Cache Miss)**:
    *   Call `create_worker(...)`.
    *   **Assert**: Операция заняла > X секунд (время билда).
    *   **Assert**: Образ создан.
3.  **Second Run (Cache Hit)**:
    *   Call `create_worker(...)` с теми же параметрами.
    *   **Assert**: Операция заняла < 1 секунды (мгновенный старт).
    *   **Assert**: ID образа совпадает с предыдущим.
    *   **Assert**: В Redis обновилось поле `last_used_at` для этого образа.

#### Scenario C: Garbage Collection (LRU)
Проверяем, что GC удаляет старые образы, но не трогает "свежие" и боевые.

1.  **Setup**:
    *   Создаем мок-образы `worker-test:old`, `worker-test:fresh`, `worker-test:unused`.
    *   Создаем боевой "noise" образ `worker:prod` (не должен быть удален).
2.  **Mock State**:
    *   В Redis подсовываем фейковые `last_used_at`:
        *   `old`: 10 дней назад (Target for GC).
        *   `fresh`: 1 час назад (Keep).
        *   `unused`: нет записи (Target for GC).
3.  **Action**: Запускаем таску `gc_images()`.
4.  **Assert**:
    *   `worker-test:old` -> **Deleted**.
    *   `worker-test:unused` -> **Deleted**.
    *   `worker-test:fresh` -> **Exists**.
    *   `worker:prod` -> **Exists** (Safety check).

---

## 4. Infrastructure Requirements

### 4.1 Pytest Fixtures
```python
@pytest.fixture
def docker_client():
    """Real docker client."""
    return docker.from_env()

@pytest.fixture
def worker_settings(monkeypatch):
    """Force test prefix."""
    monkeypatch.setenv("WORKER_IMAGE_PREFIX", "worker-test")
    monkeypatch.setenv("WORKER_DOCKER_LABELS", '{"com.codegen.environment": "test"}')
    return WorkerManagerSettings()

@pytest.fixture
def clean_docker(docker_client):
    """Cleanup test containers/images before/after."""
    # Prune containers with label=test
    # Prune images with name=worker-test*
    pass
```

### 4.2 CI Configuration
Github Actions должен запускаться с возможностью доступа к докеру (обычно по умолчанию в ubuntu-latest, либо через service container).
