# Brainstorm: Workspace Browser

> **Дата**: 2026-03-13
> **Контекст**: Хочу видеть файлы проектных воркспейсов в админке — дерево файлов и содержимое
> **Status**: done

---

## Current State

### Что уже есть

**Backend (worker-manager introspect API):**
- `GET /api/introspect/workers/{id}/tree` — полное дерево файлов workspace (рекурсивный `os.walk`)
- `GET /api/introspect/workers/{id}/files/{path}` — содержимое файла (макс 1 MB, UTF-8, path traversal protection)
- `GET /api/introspect/workers/{id}/prompts` — CLAUDE.md + TASK.md

**Frontend (admin WorkerDetailPage):**
- Вкладка "Files" на странице воркера — двухпанельный интерфейс
- Слева: дерево файлов с expand/collapse (папки первыми, алфавитная сортировка)
- Справа: просмотр файла с указанием размера

**Воркспейсы на диске:**
- Проектные: `WORKSPACE_BASE_PATH/{project_id}/workspace/` — переиспользуются между воркерами
- Scaffolded: `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/` (плоская структура)
- Ephemeral: `WORKSPACE_BASE_PATH/{worker_id}/workspace/` (для одноразовых воркеров)
- Garbage collection: каждые 6 часов, удаляет >35ч неактивные

### Ограничение текущей реализации

Всё привязано к **worker_id**. Чтобы посмотреть файлы, нужен живой воркер. Но:
- Воркспейс принадлежит **проекту**, не воркеру
- Воркер может быть уже мёртв, а воркспейс всё ещё жив на диске (до 35ч)
- Между ранами одного проекта воркспейс один и тот же

---

## Problem

Хочу видеть файлы проекта в админке без привязки к конкретному воркеру. Минимум: дерево файлов + просмотр содержимого.

---

## Options

### Option A: Новые эндпоинты в worker-manager (по project_id)

Добавить в introspect API:
- `GET /api/introspect/workspaces/{project_id}/tree`
- `GET /api/introspect/workspaces/{project_id}/files/{path}`

Логика определения пути:
1. Проверить `WORKSPACE_BASE_PATH/{project_id}/workspace/` (основной путь)
2. Если нет — найти repo_id проекта (через API или Redis) и проверить `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/`
3. 404 если ничего нет

На фронте — вкладка "Workspace" на странице проекта (`/projects/:id`).

- (+) Минимум работы — переиспользуем 90% кода из worker introspection
- (+) Worker-manager уже имеет доступ к filesystem и Docker volumes
- (+) Path traversal protection уже написана (`_safe_resolve`)
- (+) Frontend компоненты дерева и вьюера уже есть — выносим в shared component
- (-) Worker-manager становится ещё более нагруженным (но endpoint лёгкий)

### Option B: Эндпоинты в основном API

Добавить workspace browsing в API-сервис (port 8000).

- (+) Всё в одном сервисе, проще проксировать
- (-) API-сервис **не имеет доступа к filesystem** воркспейсов — они на хосте, монтируются только в worker-manager
- (-) Нужно либо монтировать volumes в API, либо делать internal HTTP к worker-manager (лишний слой)
- (-) Нарушает принцип: worker-manager = единственный сервис с доступом к Docker/workspace

### Option C: Git-based browsing (через GitHub API)

Показывать файлы из GitHub-репозитория проекта.

- (+) Не зависит от того, жив ли воркспейс на диске
- (+) Доступен всегда (после push)
- (-) Показывает только закоммиченное, не текущее состояние (может быть далеко от реального)
- (-) Задержка: изменения видны только после push
- (-) GitHub API rate limits
- (-) Не заменяет просмотр workspace — другой use case

---

## Решение (после обсуждения)

**Option A** с рефакторингом: workspace как первичная сущность, воркер ссылается на него.

### Архитектурное изменение

Сейчас: `Worker → files` (tree/files привязаны к worker_id)
Должно быть: `Worker → Workspace → files` (workspace — самостоятельная сущность по project_id)

Это значит:
1. **Новые эндпоинты** workspace browsing по `project_id` — основной способ просмотра файлов
2. **Worker detail** — вместо собственных tree/files показывает workspace своего проекта (ссылка/redirect)
3. **Убираем дублирование** — один набор компонентов FileTree/FileViewer, одна точка входа

### API дизайн

```
# Workspace endpoints (новые, первичные)
GET /api/introspect/workspaces/{project_id}/tree
GET /api/introspect/workspaces/{project_id}/files/{path}

# Worker endpoints (рефакторинг)
GET /api/introspect/workers/{id}/tree      → redirect/proxy к workspaces/{project_id}/tree
GET /api/introspect/workers/{id}/files/... → redirect/proxy к workspaces/{project_id}/files/...
GET /api/introspect/workers/{id}/prompts   → остаётся (CLAUDE.md/TASK.md — per-worker)
```

### Scope

**Backend (worker-manager):**
1. Новый роутер `workspaces.py` (~80 LOC):
   - `GET /workspaces/{project_id}/tree` — `os.walk` по workspace path
   - `GET /workspaces/{project_id}/files/{path}` — чтение файла
   - Резолв пути: `WORKSPACE_BASE_PATH/{project_id}/workspace/` → fallback `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/`
   - Переиспользует `_safe_resolve` из introspect
2. Worker tree/files → делегируют в workspace эндпоинты (по `project_id` из Redis meta)
3. `/prompts` остаётся на воркере (CLAUDE.md/TASK.md — контекст конкретного рана)

**Frontend:**
1. Вынести `FileTree` и `FileViewer` из WorkerDetailPage в `components/workspace/`
2. Вкладка "Workspace" на ProjectDetailPage — основной способ просмотра
3. WorkerDetailPage вкладка "Files" → показывает тот же workspace component (по project_id воркера)
4. Nginx: `/wm-api/` proxy уже настроен — новые эндпоинты пойдут автоматически

**Бонусы (не первая итерация):**
- Syntax highlighting (Prism.js / Shiki) — сейчас plain text
- Diff view (изменения от последнего коммита)
- git log/blame интеграция
- Скачивание файлов / архив workspace

### Реалистичность

Очень реально. Вся инфраструктура на месте:
- worker-manager + introspect API + nginx proxy
- Компоненты дерева и вьюера уже работают
- По сути рефакторинг: отвязать от worker_id, привязать к project_id, воркер делегирует

---

## Action Items

- → new task: "Workspace browser — workspace как первичная сущность с project-level browsing"
  - Backend: новый роутер workspaces.py (tree + files по project_id), worker tree/files делегируют
  - Frontend: shared FileTree/FileViewer components, Workspace tab на ProjectDetailPage, worker Files tab ссылается на workspace
  - Worker prompts (CLAUDE.md/TASK.md) остаются per-worker
