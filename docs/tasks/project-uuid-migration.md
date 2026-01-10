# Project UUID Migration

> **Приоритет**: High
> **Оценка**: ~2 часа
> **Статус**: Planning

## Проблема

Сейчас project ID создаётся по-разному в разных местах:

| Источник | Формат ID | Пример |
|----------|-----------|--------|
| CLI `orchestrator project create` | UUID (8 chars) | `3a2b1c4d` |
| LangGraph `create_project` tool | UUID (8 chars) | `f7e8d9c0` |
| Scheduler `github_sync.py` | repo_name | `hello-world-bot` |

Это приводит к путанице:
- Проект `hello-world-bot` был обнаружен scheduler'ом из GitHub
- Репо удалили, но запись осталась
- PO пытается работать с этим проектом, но репо не существует

## Решение

Унифицировать: **везде UUID**.

### Изменения

#### 1. Scheduler: `github_sync.py`

```python
# Было (line 234-239):
project = Project(
    id=repo_name,  # <-- проблема
    name=repo_name,
    github_repo_id=repo_id,
    status=ProjectStatus.DISCOVERED.value,
)

# Стало:
import uuid
project = Project(
    id=str(uuid.uuid4())[:8],  # UUID
    name=repo_name,            # name отдельно для отображения
    github_repo_id=repo_id,
    status=ProjectStatus.DISCOVERED.value,
)
```

#### 2. Проверить все места использования ID

- [ ] `services/scheduler/src/tasks/github_sync.py:234` - основное изменение
- [ ] `services/langgraph/src/tools/projects.py:73` - уже UUID ✅
- [ ] `shared/cli/src/orchestrator/commands/project.py:85` - уже UUID ✅
- [ ] `services/api/src/routers/projects.py` - принимает любой string, OK

#### 3. Миграция существующих данных

Опционально: скрипт для конвертации существующих non-UUID ID в UUID.

```sql
-- Проверить проекты с non-UUID ID
SELECT id, name FROM projects
WHERE id !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){0,3}$';
```

## Acceptance Criteria

- [ ] Все новые проекты создаются с UUID ID
- [ ] `name` используется для отображения, `id` — только для связей
- [ ] Scheduler не создаёт проекты с `id=repo_name`

## Файлы для изменения

1. `services/scheduler/src/tasks/github_sync.py` - генерация UUID
2. Опционально: миграция БД для существующих записей

## Риски

- Существующие проекты с non-UUID ID могут сломаться
- Foreign keys в `port_allocations`, `tasks` зависят от project_id

## Рекомендация

Для MVP: просто удалить проект `hello-world-bot` из БД и не мигрировать старые данные.

```sql
DELETE FROM tasks WHERE project_id = 'hello-world-bot';
DELETE FROM port_allocations WHERE project_id = 'hello-world-bot';
DELETE FROM projects WHERE id = 'hello-world-bot';
```
