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

#### Scenario D: Configuration & File System Verification (Mock Docker + Exec)
Проверяем корректность инициализации файловой системы и инструментов внутри контейнера.

1.  **Variations Setup**:
    *   Case 1: `agent_type=CLAUDE`, `capabilities=["GIT"]`
    *   Case 2: `agent_type=FACTORY`, `capabilities=[]`
2.  **Action**: `create_worker(...)`.
3.  **Assert (Container Inspection)**:
    *   Через `docker_client.containers.get(id).exec_run("ls -la")` или `inspect`:
    *   **Case 1**:
        *   Файл `/app/CLAUDE.md` существует.
        *   Бинарник `claude` доступен в PATH.
        *   Бинарник `git` доступен.
        *   `orchestrator-cli` доступен.
    *   **Case 2**:
        *   Файл `/app/AGENTS.md` существует.
        *   Бинарник `factory` доступен.
        *   Бинарник `git` **отсутствует**.
4.  **Assert (Env Vars)**:
    *   `ORCHESTRATOR_URL` прокинут корректно.
    *   `ALLOWED_COMMANDS` соответствует переданным.
    *   **System Prompt**: В инструкциях агента (env var или file) есть требование: *"Wrap final result map in <result>...</result> tags"*.

#### Scenario E: Orchestrator Integration & Output Protocol (Contract Test)
Проверяем цепочку: `Worker Wrapped -> Agent -> Orchestrator CLI -> System` и парсинг результата.

1.  **Setup**:
    *   Запускаем `worker-manager` и `mock-anthropic-server`.
    *   Настраиваем `ALLOWED_COMMANDS=["project.get"]`.

2.  **Action (Happy Path with Result)**:
    *   Отправляем в Input Queue промпт: *"Get project info"*.
    *   **Mock LLM** настроен отвечать:
        ```xml
        Ok, I found it.
        <result>
        {
          "status": "success",
          "summary": "Project found",
          "data": {"id": "123"}
        }
        </result>
        ```
    *   **Assert (Output Queue)**:
        *   Сообщение валидируется как `WorkerResult`.
        *   `verdict.status` == `success`.
        *   `verdict.data.id` == `123`.
        *   `exit_code` == 0.

3.  **Action (Agent Failure / No Result)**:
    *   **Mock LLM** отвечает текстом без тегов `<result>`.
    *   **Assert**:
        *   `WorkerResult` получен.
        *   `verdict` == `None` (или status=`failure` в зависимости от реализации).
        *   `error` содержит "Failed to parse agent verdict".

4.  **Action (Blocked Call)**:
    *   Отправляем промпт: *"Delete server 1"*.
    *   **Mock LLM** пытается вызвать `orchestrator infra delete 1`.
    *   **Assert**: В Output Queue ошибка прав доступа (Standard CLI Permission Error).

#### Scenario F: Status Lifecycle (Happy Path & Crash)
Проверяем корректность переходов состояний в Redis при разных жизненных ситуациях.

1.  **STARTING**: Отправляем `create`.
    *   **Assert**: Redis `worker:status:{id}` == `STARTING` (сразу после приема команды).
2.  **RUNNING**: Ждем (polling 1-2 sec).
    *   **Assert**: Redis `worker:status:{id}` == `RUNNING`.
3.  **FAILED (Simulated Crash)**:
    *   **Action**: `docker_client.containers.get(id).kill()`.
    *   **Assert**: Redis `worker:status:{id}` переходит в `FAILED` (или `STOPPED` с exit_code!=0).
    *   **Assert**: Поле `error` в Redis заполнено.

#### Scenario G: Graceful Deletion (Manual Stop)
Проверяем команду явного удаления.

1.  **Setup**: Запускаем воркера, ждем `RUNNING`.
2.  **Action**: Отправляем `delete` в `worker:commands`.
3.  **Assert (Container)**:
    *   `docker ps` не показывает контейнер.
    *   `docker inspect` (если еще есть) показывает `Status=exited`.
4.  **Assert (Redis Status)**:
    *   Redis `worker:status:{id}` == `STOPPED`.
    *   Ключ **не удален**, чтобы клиенты (Telegram Bot) могли узнать финальный статус.

#### Scenario H: Idle Pause (Safety Check)
Проверяем механизм постановки на паузу при бездействии. Важно: `IDLE_TIMEOUT` должен быть строго больше `TASK_TIMEOUT`.

1.  **Safety Check**:
    *   **Action**: Проверяем конфиг `worker-manager`.
    *   **Assert**: `IDLE_TIMEOUT_SECONDS` > `TASK_EXECUTION_TIMEOUT_SECONDS`. (Например, 35 мин > 30 мин). Это гарантирует, что мы не уснем посередине долгой задачи.
2.  **Idle Detection**:
    *   **Setup**: Worker `RUNNING`.
    *   **Action**: Форсированно обновляем в Redis `last_activity` на `NOW - IDLE_TIMEOUT - 1s`.
    *   **Action**: Запускаем таску мониторинга `pause_idle_workers()`.
3.  **Assert**:
    *   `docker_client.containers.get(id).status` == `paused`.
    *   Redis `worker:status:{id}` == `PAUSED`.

#### Scenario I: Auto-Wakeup
Проверяем, что воркер просыпается при поступлении новых сообщений.

1.  **Setup**: Worker `PAUSED`.
2.  **Action**: Публикуем сообщение в `worker:{type}:{id}:input`.
3.  **Action**: Запускаем таску мониторинга `wakeup_workers()` (или ждем loop).
4.  **Assert (Wakeup)**:
    *   `docker_client.containers.get(id).status` == `running`.
    *   Redis `worker:status:{id}` == `RUNNING`.
5.  **Assert (Processing)**:
5.  **Assert (Processing)**:
    *   Воркер успешно вычитал сообщение.
    *   В `output` пришел валидный `WorkerResult` с отчетом.

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
