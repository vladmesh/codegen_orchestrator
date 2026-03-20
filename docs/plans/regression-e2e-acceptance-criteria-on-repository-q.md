# Regression E2E: acceptance criteria on Repository + QA report in admin UI

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Кнопка "Run E2E" в админке — чёрная дыра: QA не знает что тестировать (story_id пустой при standalone), отчёт нигде не показывается, знания не накапливаются между сторями. Решение: acceptance_criteria на Repository (живой документ, растёт с каждой сторей), архитектор обновляет при декомпозиции, QA тестирует по полному списку (регрессия), отчёт виден в админке.

### Текущее состояние
- `Repository` модель: нет поля acceptance_criteria
- `RepositoryUpdate` DTO: PATCH поддерживает exclude_unset — достаточно добавить поле
- `LanggraphAPIClient`: нет методов get_repository/patch_repository — нужно добавить
- Architect tools: нет инструмента для обновления репо — нужен `update_acceptance_criteria`
- QA consumer: строит промпт из `story_description` (пустой при standalone) — переключить на repo.acceptance_criteria
- `build_qa_prompt()`: принимает story_description — рефакторить на acceptance_criteria
- Admin frontend ApplicationDetailPage: нет отображения QA run результатов
- Runs API: фильтрация по run_type=qa уже работает — фронтенд может использовать
- `run_e2e` endpoint: не передаёт bot_username — нужно доставать из проекта

## Steps

1. [ ] DB migration + model + DTOs: acceptance_criteria на Repository ⚠️ needs-approval
   - **Input**: `shared/models/repository.py`, `shared/contracts/dto/repository.py`, alembic
   - **Output**: 
     - `Repository.acceptance_criteria` (Text, nullable) в модели
     - `RepositoryDTO` возвращает acceptance_criteria
     - `RepositoryUpdate` принимает acceptance_criteria (optional)
     - Alembic миграция `add_acceptance_criteria_to_repositories`
   - **Test**: Service test: создать repo через API, PATCH acceptance_criteria, GET — значение сохранилось

2. [ ] LanggraphAPIClient: методы для работы с repository
   - **Input**: `services/langgraph/src/clients/api.py`
   - **Output**: 
     - `get_repository(repo_id: str) -> dict` — GET /repositories/{repo_id}
     - `update_repository(repo_id: str, payload: dict) -> dict` — PATCH /repositories/{repo_id}
   - **Test**: Unit test: мок httpx, проверить URL и payload

3. [ ] Architect tool: update_acceptance_criteria
   - **Input**: `services/langgraph/src/agents/architect/tools.py`, `services/langgraph/src/prompts/architect/__init__.py`
   - **Output**: 
     - Новый tool `update_acceptance_criteria(project_id, criteria)` — резолвит primary repo, PATCH-ит acceptance_criteria
     - Обновлённый architect prompt: инструкция вызывать tool после создания тасков — дописать новые проверки, убрать устаревшие
     - Tool читает текущие criteria (может быть пустой), возвращает обновлённый список
   - **Test**: Unit test: мок api_client, проверить что tool вызывает get_primary_repository → update_repository с корректным payload

4. [ ] QA consumer: переключить на acceptance_criteria
   - **Input**: `services/langgraph/src/consumers/qa.py`, `services/langgraph/src/consumers/_qa_runner.py`
   - **Output**: 
     - QA consumer: получает application.repo_id → get_repository → acceptance_criteria
     - `build_qa_prompt(acceptance_criteria, deployed_url, bot_username)` вместо story_description
     - Если acceptance_criteria пустой — fail-fast (нет смысла запускать QA без критериев)
     - `run_e2e` endpoint: резолвить bot_username из project.config.modules (если есть tg_bot)
   - **Test**: Unit test: build_qa_prompt с acceptance_criteria генерирует корректный промпт. Unit test: пустые criteria → RuntimeError

5. [ ] Admin frontend: QA run status + отчёт на ApplicationDetailPage
   - **Input**: `services/admin-frontend/src/pages/ApplicationDetailPage.tsx`, `services/admin-frontend/src/types/api.ts`
   - **Output**: 
     - Тип `Run` в types/api.ts (id, type, status, result, created_at, completed_at)
     - Запрос последнего QA run: `GET /runs/?run_type=qa&limit=1` с фильтром по run_metadata.application_id
     - На странице Application: badge со статусом последнего QA (pending/passed/failed/error)
     - Раскрывающийся блок с деталями: checks (pass/fail list), summary, report (markdown)
     - Auto-refetch каждые 10 секунд пока run в статусе QUEUED/RUNNING
   - **Test**: Ручная проверка в браузере (фронтенд без автотестов)

6. [ ] Runs API: фильтрация по application_id
   - **Input**: `services/api/src/routers/runs.py`
   - **Output**: 
     - Параметр `application_id: int | None` в GET /runs/ — фильтрует по run_metadata->application_id
     - Либо: endpoint GET /applications/{app_id}/runs?type=qa (проще для фронтенда)
   - **Test**: Service test: создать run с metadata.application_id, запросить по фильтру — найден

7. [ ] Seed acceptance_criteria для существующих репозиториев
   - **Input**: Список существующих репозиториев с задеплоенными приложениями (через API)
   - **Output**: 
     - Скрипт или ручной PATCH для каждого repo с реальными acceptance_criteria
     - Критерии на основе того что приложения реально умеют (проверить через SSH или документацию)
   - **Test**: GET /repositories/{id} — acceptance_criteria заполнены для всех задеплоенных проектов

8. [ ] Integration test: полный E2E flow
   - **Input**: Все предыдущие шаги
   - **Output**: 
     - Service test: создать project + repo с acceptance_criteria + application → POST /applications/{id}/run-e2e → проверить что QAMessage содержит criteria (мок Redis)
     - Проверить что build_qa_prompt строит промпт из criteria
   - **Test**: Интеграционный тест покрывающий API → QAMessage → prompt pipeline

