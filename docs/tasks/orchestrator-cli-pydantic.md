# Orchestrator CLI: Pydantic-first Design

**Цель**: Сделать orchestrator CLI единственным интерфейсом агента к системе с автоматической валидацией через Pydantic и понятными ошибками.

**Дата создания**: 2026-01-09
**Статус**: Planning

---

## Проблема

1. Агент видит CLI документацию, но команды неполные
2. Агент лезет в curl напрямую → получает непонятные 422
3. Нет единой точки входа с хорошими ошибками
4. Агент может обходить валидацию

## Целевая Архитектура

```
┌─────────────────────────────────────────────────────┐
│                   CLI Agent (Claude, Factory)       │
│  (ТОЛЬКО orchestrator CLI, никаких curl/redis)      │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│              orchestrator CLI (Typer + Pydantic)    │
│  • Pydantic валидация на входе                      │
│  • Автоматические человекочитаемые ошибки           │
│  • Скрывает детали (генерит ID, форматирует)        │
│  • Единственный интерфейс к системе                 │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                 API / Redis                          │
└─────────────────────────────────────────────────────┘
```

## Принципы

1. **Никаких curl/redis в промпте** — только CLI
2. **Fail fast с автоматической ошибкой** — Pydantic ValidationError
3. **CLI генерирует всё что можно** — ID, timestamps, defaults
4. **Минимум обязательных аргументов** — разумные defaults
5. **Zero boilerplate** — декоратор для валидации

---

## Итеративный План

### Phase 1: Базовая инфраструктура валидации

**Цель**: Создать переиспользуемый паттерн Pydantic + Typer

**Задачи**:
- [ ] Создать `services/universal-worker/orchestrator_cli/validation.py`
  - Декоратор `@validate(Model)` для автоматической валидации
  - Красивый вывод ошибок Pydantic (field: message)
  - Exit code 1 при ошибке валидации
- [ ] Создать базовые модели в `orchestrator_cli/models/`
  - `ProjectCreate`, `ProjectUpdate`
  - `SecretSet`
  - `EngineeringTask`
- [ ] Написать тесты на валидацию

**Пример кода**:
```python
# validation.py
from functools import wraps
from pydantic import ValidationError
import typer

def validate(model_class):
    """Декоратор: авто-валидация через Pydantic."""
    def decorator(func):
        @wraps(func)
        def wrapper(**kwargs):
            try:
                validated = model_class(**kwargs)
                return func(validated)
            except ValidationError as e:
                for err in e.errors():
                    loc = ".".join(str(x) for x in err["loc"])
                    typer.echo(f"✗ {loc}: {err['msg']}", err=True)
                raise typer.Exit(1)
        return wrapper
    return decorator
```

**Definition of Done**:
- `@validate(Model)` работает
- Pydantic ошибки выводятся красиво
- Есть unit тесты

---

### Phase 2: Project Commands

**Цель**: Полноценное управление проектами через CLI

**Команды**:
```bash
orchestrator project create --name NAME [--type telegram-bot] [--description DESC]
orchestrator project list [--status STATUS]
orchestrator project get PROJECT_ID
orchestrator project delete PROJECT_ID
orchestrator project set-secret PROJECT_ID KEY VALUE
```

**Задачи**:
- [ ] Модель `ProjectCreate`:
  ```python
  class ProjectCreate(BaseModel):
      name: str = Field(..., min_length=1, max_length=100)
      type: Literal["telegram-bot", "web-app", "api"] = "telegram-bot"
      description: str | None = None
      # id генерируется автоматически
  ```
- [ ] Модель `SecretSet`:
  ```python
  class SecretSet(BaseModel):
      project_id: str
      key: str = Field(..., pattern=r"^[A-Z_]+$")  # TELEGRAM_TOKEN, etc.
      value: str = Field(..., min_length=1)
  ```
- [ ] Реализовать команды в `orchestrator_cli/commands/project.py`
- [ ] CLI сам генерирует UUID для project_id
- [ ] Интеграция с API через httpx

**Definition of Done**:
- Агент может создать проект одной командой
- Агент может сохранить токен как секрет
- Ошибки валидации понятны

---

### Phase 3: Engineering Commands

**Цель**: Запуск и мониторинг engineering tasks

**Команды**:
```bash
orchestrator engineering trigger PROJECT_ID --task "Create hello world bot"
orchestrator engineering status TASK_ID [--follow]
orchestrator engineering logs TASK_ID
```

**Задачи**:
- [ ] Модель `EngineeringTask`:
  ```python
  class EngineeringTask(BaseModel):
      project_id: str
      task: str = Field(..., min_length=10, max_length=2000)
      priority: Literal["low", "normal", "high"] = "normal"
  ```
- [ ] Реализовать команды
- [ ] `--follow` для streaming статуса

**Definition of Done**:
- Агент может запустить engineering task
- Агент может следить за статусом

---

### Phase 4: Deploy Commands

**Цель**: Управление деплоями

**Команды**:
```bash
orchestrator deploy trigger PROJECT_ID [--server SERVER_ID]
orchestrator deploy status JOB_ID
orchestrator deploy logs JOB_ID
```

**Задачи**:
- [ ] Модели `DeployStart`, `DeployStatus`
- [ ] Реализовать команды
- [ ] Интеграция с deploy queue

---

### Phase 5: Respond Command (Fix)

**Цель**: Починить или убрать двойной канал общения

**Варианты**:
1. **Убрать respond** — агент просто пишет текст, JSON output работает
2. **Починить respond** — для async notifications (deploy done, etc.)

**Задачи**:
- [ ] Решить какой вариант
- [ ] Реализовать
- [ ] Обновить промпт

---

### Phase 6: Обновление PO промпта

**Цель**: Промпт содержит только CLI команды, никаких curl/API

**Задачи**:
- [ ] Убрать любые упоминания curl, API endpoints
- [ ] Добавить примеры использования CLI
- [ ] Добавить примеры ошибок и как их исправить:
  ```
  # Пример ошибки:
  $ orchestrator project create --name ""
  ✗ name: String should have at least 1 character

  # Правильно:
  $ orchestrator project create --name "my-bot"
  ✓ Project created: my-bot (id: abc-123)
  ```
- [ ] Обновить `shared/prompts/po_agent.yml`

---

## Файловая Структура

```
services/universal-worker/
├── orchestrator_cli/
│   ├── __init__.py
│   ├── main.py              # Typer app entry point
│   ├── validation.py        # @validate декоратор
│   ├── models/
│   │   ├── __init__.py
│   │   ├── project.py       # ProjectCreate, SecretSet
│   │   ├── engineering.py   # EngineeringTask
│   │   └── deploy.py        # DeployStart
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── project.py
│   │   ├── engineering.py
│   │   ├── deploy.py
│   │   └── respond.py
│   └── client.py            # HTTP client для API
└── tests/
    └── test_validation.py
```

---

## Метрики Успеха

1. **Агент НЕ использует curl** — только orchestrator CLI
2. **Все ошибки понятны** — Pydantic автоматически
3. **Zero boilerplate** — добавление новой команды = модель + 10 строк кода
4. **E2E US1 работает** — создание бота с токеном через CLI

---

## Зависимости

- Typer >= 0.9.0
- Pydantic >= 2.0
- httpx (async HTTP client)
