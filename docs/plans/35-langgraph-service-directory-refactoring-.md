# #35 LangGraph service directory refactoring (workers→consumers)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# LangGraph Service Refactoring Plan

## Проблема / Problem Statement
В текущей структуре директорий сервиса `langgraph` присутствует значительная путаница в терминологии и логике распределения файлов:
1. `workers/` — содержит Redis-консьюмеров (`engineering_worker.py`, `deploy_worker.py`), которые запускают подграфы. Называть их воркерами некорректно, так как по `GLOSSARY.md` воркер — это отдельный эфемерный Docker-контейнер (Developer Worker).
2. `worker/` — содержит `main.py`, `provisioner.py`, `events.py`. Это точка входа и общие обработчики событий сервиса `langgraph`, а не "воркер". Расположение `main.py` внутри папки `worker` нелогично.
3. `prompts/` — содержит только инструкции для `developer_worker` (эфемерного докер-контейнера). Промпты для PO хранятся локально в `src/po/prompts.py`. Нет единого подхода.

## Целевая структура директорий

```text
services/langgraph/src/
├── main.py                  # Перенос из src/worker/main.py (точка входа)
├── clients/                 # Без изменений
├── config/                  # Без изменений
├── consumers/               # Переименовано из `workers/`. Консьюмеры очередей Redis.
│   ├── engineering.py       # Бывший engineering_worker.py
│   ├── deploy.py            # Бывший deploy_worker.py
│   └── ...
├── events.py                # Перенос из src/worker/events.py (один файл — не нужна директория)
├── provisioner.py           # Перенос из src/worker/provisioner.py (один файл)
├── llm/                     # Без изменений
├── po/                      # Остаётся на месте (единственный агент — agents/ избыточен)
├── subgraphs/               # Без изменений
├── nodes/                   # Без изменений
├── state/                   # Без изменений
├── schemas/                 # Без изменений
└── prompts/                 # Единое хранилище всех промптов
    ├── developer_worker/    # Остаётся — INSTRUCTIONS.md
    └── po/                  # Перенос промптов из src/po/prompts.py
```

**Принципы (отличия от исходного плана):**
- `events.py` и `provisioner.py` — одиночные файлы, отдельные директории для них избыточны. Создадим директории когда разрастутся.
- `po/` остаётся в `src/po/` — создавать `agents/po/` ради единственного агента преждевременно. Перенесём когда появится второй агент.
- `main.py` переносится в корень `src/` — это точка входа сервиса, а не "ещё один consumer".

## Унификация терминов (в GLOSSARY.md)
* **Consumer**: Модуль `langgraph`, который слушает очередь Redis (`engineering:queue`, `deploy:queue`, `po:input`). Перестаём называть консьюмеры внутри langgraph словом "workers".
* **Worker**: СТРОГО эфемерный Docker-контейнер (`Developer Worker`) с CLI-агентом (Claude/Factory).
* **Agent**: LangGraph агент (PO), который принимает решения и делегирует в Subgraphs.
* **Subgraph**: Подграф LangGraph, реализующий бизнес-процесс (Engineering, DevOps).

## Шаги рефакторинга

Порядок выбран по принципу минимального риска — сначала переименования, которые не трогают PO.

### Шаг 1: Rename `workers/` → `consumers/`
- Переименовать `src/workers/` → `src/consumers/`
- Убрать суффиксы `_worker` у файлов: `engineering_worker.py` → `engineering.py`, `deploy_worker.py` → `deploy.py`
- Обновить все импорты в коде
- Обновить пути в тестах (`tests/unit/`, `tests/integration/`)

### Шаг 2: Restructure `worker/` module
- Перенести `src/worker/main.py` → `src/main.py`
- Перенести `src/worker/events.py` → `src/events.py`
- Перенести `src/worker/provisioner.py` → `src/provisioner.py`
- Обновить все импорты
- Удалить директорию `src/worker/`
- Обновить пути в тестах

### Шаг 3: Update entrypoints
- Обновить `Dockerfile`: CMD ссылки на `src.worker.main` → `src.main`
- Обновить `docker-compose.yml`: command для `engineering-worker` и `deploy-worker` (`src.workers.engineering_worker` → `src.consumers.engineering`)
- Обновить `pyproject.toml` если есть ссылки на старые пути

### Шаг 4: Centralize prompts
- Переместить `src/po/prompts.py` → `src/prompts/po/` (как .py или .md — по содержимому)
- Обновить импорты в `src/po/graph.py`

### Шаг 5: Update documentation
- Обновить GLOSSARY.md с унифицированными терминами
- Обновить ARCHITECTURE.md если есть ссылки на старые пути
- Обновить CLAUDE.md (architecture section)
- Удалить этот файл (REFACTORING_PLAN.md) — план перенесён в таску
