# Project Scaffolding Service

> **Статус**: 🔵 Ready for implementation  
> **Приоритет**: High — критичная оптимизация

## Проблема

Сейчас при создании проекта **LLM-агент** (Claude Code) должен:
1. Понять что нужно использовать copier
2. Парсить инструкцию из промпта
3. Запустить `copier copy gh:vladmesh/service-template . --data ...`

Это **неэффективно**:
- Тратит токены на детерминированную операцию
- Рискует ошибками парсинга
- Усложняет задачу для агента

## Решение

Автоматически запускать copier **до** того как работа дойдёт до LLM-агента.

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────┐
│   API       │────►│ Scaffolder Service   │────►│ Claude Code │
│ (create     │     │ (git + copier)       │     │ (бизнес-    │
│  project)   │     │ автоматически        │     │  логика)    │
└─────────────┘     └──────────────────────┘     └─────────────┘
```

**Плюсы**:
- ✅ Экономия токенов — агенту не нужно разбираться с copier
- ✅ Надёжность — детерминированный код вместо LLM
- ✅ Скорость — меньше работы для агента
- ✅ Простота — агент получает готовую структуру

### Синхронизация

**Выбранный подход**: API не ждёт, но DeveloperNode проверяет готовность.

```
API ──► fire-and-forget ──► Scaffolder (async)
                               │
                               ▼
                          project.status = "scaffolded"
                               │
DeveloperNode ◄─── poll/check ─┘
```

1. **API** отправляет задание в `scaffolder:queue` и сразу отвечает клиенту
2. **Scaffolder** после успешной работы обновляет `project.status = "scaffolded"` через API
3. **DeveloperNode** перед началом работы проверяет статус проекта и ждёт `scaffolded`

---

## Изменения

### 1. Enum модулей (shared)

**Файл**: `shared/schemas/modules.py` [NEW]

```python
from enum import Enum

class ServiceModule(str, Enum):
    """Available modules for project scaffolding."""
    
    BACKEND = "backend"        # Always required
    TG_BOT = "tg_bot"         # Telegram bot
    NOTIFICATIONS = "notifications"  # Notifications worker
    FRONTEND = "frontend"      # Frontend service
```

---

### 2. API: Добавить modules в ProjectCreate

**Файл**: `services/api/src/schemas.py`

```python
from shared.schemas.modules import ServiceModule

class ProjectCreate(BaseModel):
    id: str
    name: str
    status: str = "pending"
    config: dict | None = None
    modules: list[ServiceModule] = [ServiceModule.BACKEND]  # NEW
```

**Файл**: `services/api/src/routers/projects.py`

После provision_project_repo — отправить задание в `scaffolder:queue`:

```python
# После создания репо — запустить scaffolding
redis_client.xadd("scaffolder:queue", {
    "repo_full_name": f"{org}/{repo_name}",
    "project_name": project_in.name,
    "modules": ",".join(m.value for m in project_in.modules),
})
```

---

### 3. Новый сервис: Scaffolder [NEW]

**Директория**: `services/scaffolder/`

#### Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN pip install copier==9.4.1 redis pyyaml structlog

COPY src /app
WORKDIR /app

CMD ["python", "main.py"]
```

#### Логика

```python
# main.py
async def process_scaffolding(job: dict):
    repo = job["repo_full_name"]
    modules = job["modules"]
    
    # 1. Clone repo
    subprocess.run(["git", "clone", f"https://x:$GITHUB_TOKEN@github.com/{repo}", "/tmp/repo"])
    
    # 2. Run copier
    subprocess.run([
        "copier", "copy", "gh:vladmesh/service-template", "/tmp/repo",
        "--data", f"project_name={job['project_name']}",
        "--data", f"modules={modules}",
        "--trust", "--defaults"
    ])
    
    # 3. Commit & push
    subprocess.run(["git", "add", "."], cwd="/tmp/repo")
    subprocess.run(["git", "commit", "-m", "feat: scaffold project"], cwd="/tmp/repo")
    subprocess.run(["git", "push"], cwd="/tmp/repo")
```

#### Redis Queue

Слушает: `scaffolder:queue` (Redis Stream)

---

### 4. DeveloperNode: Обновить промпт

**Файл**: `services/langgraph/src/nodes/developer.py`

Обновить `_build_task_message()`:

```diff
- ### 2. Scaffold Project Structure
- Use copier to create project structure...
+ ### 2. Project Structure (already scaffolded)
+ The project structure is already created with copier.
+ Focus on implementing the business logic.
```

> [!IMPORTANT]
> **Добавить документацию по service-template в промпт**
> 
> Агент должен понимать структуру scaffolded проекта:
> - Что такое `shared/spec/models.yaml` и `events.yaml` — описание доменных моделей и событий
> - Как работает code generation (`make generate`)
> - Структура сервисов: `src/app/`, `src/controllers/`, `src/handlers/`
> - Как relay сообщения между сервисами через Redis Streams
> 
> Документацию взять из `service-template/AGENTS.md` или добавить краткий справочник прямо в промпт.

---

### 5. workers-spawner: Убрать copier capability ✅

**Удалено**:
- `services/workers-spawner/.../capabilities/copier.py`
- `COPIER` из `CapabilityType` enum
- Тесты `TestCopierCapability`

> Scaffolding теперь обрабатывается отдельным scaffolder сервисом

---

## Docker Compose

```yaml
scaffolder:
  build: ./services/scaffolder
  environment:
    - REDIS_HOST=redis
    - GITHUB_APP_ID=${GITHUB_APP_ID}
  volumes:
    - ./keys:/app/keys:ro
  depends_on:
    - redis
```

---

## План реализации

### Итерация 1: Enum и API (30 min) ✅

- [x] Создать `shared/schemas/modules.py` с `ServiceModule` enum
- [x] Обновить `ProjectCreate` schema добавить `modules` field
- [x] Обновить `projects.py` router — сохранять modules в config

### Итерация 2: Scaffolder Service (1 hour) ✅

- [x] Создать `services/scaffolder/` структуру
- [x] Написать Dockerfile (python + git + copier)
- [x] Написать main.py с Redis Stream consumer
- [x] Добавить в docker-compose.yml

### Итерация 3: Интеграция (30 min) ✅

- [x] API → Scaffolder: отправка задания после provision (fire-and-forget)
- [x] Scaffolder → API: обновление `project.status = "scaffolded"` после успеха
- [x] DeveloperNode: добавить проверку `project.status == "scaffolded"` перед началом
- [x] Retry/timeout если scaffolding не завершился за N минут (5 min timeout)

### Итерация 4: DeveloperNode (15 min) ✅

- [x] Обновить промпт — убрать copier инструкции
- [x] Агент получает готовый scaffolded проект

### Итерация 5: Тестирование (30 min)

- [ ] Unit tests для Scaffolder
- [ ] E2E: создать проект через API → проверить repo содержит scaffold
- [ ] E2E: engineering flow — агент работает с готовой структурой

---

## Verification Plan

### Unit Tests

```bash
# Scaffolder service tests
docker compose run --rm scaffolder pytest tests/
```

### Integration Test

1. Запустить стек: `make up`
2. Создать проект через API:
   ```bash
   curl -X POST http://localhost:8000/api/projects/ \
     -H "Content-Type: application/json" \
     -d '{"id": "test-123", "name": "my-test-project", "modules": ["backend", "tg_bot"]}'
   ```
3. Проверить что в GitHub repo появились файлы от copier:
   - `services/backend/`
   - `services/tg_bot/`
   - `Makefile`
   - `.copier-answers.yml`

### Manual E2E

1. Создать проект через Telegram бота
2. Убедиться что DeveloperNode получает уже scaffolded repo
3. Агент пишет только бизнес-логику, не вызывает copier
