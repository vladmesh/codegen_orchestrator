# Admin SPA — LLM Tracing page (Langfuse iframe)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 3 задача: добавить страницу LLM Tracing (Langfuse iframe) в админку. Расширена: добавить сущность Users (список + детали), т.к. переписка с PO идёт в разрезе юзера, а не проекта.

**Текущее состояние:**
- Langfuse infra развёрнута (task-a51fb1cf done), nginx proxy `/langfuse/` → `langfuse-web:3000` работает
- LangChain → Langfuse интеграция включена (task-300f55e6 done), трейсы идут
- Users API готов: `GET /users/`, `GET /users/{id}`, `GET /users/by-telegram/{telegram_id}`
- Project модель имеет `owner_id` FK → `users.id`
- Sidebar уже содержит disabled пункт "LLM Tracing" с иконкой BrainCircuit
- LogsPage — рабочий паттерн iframe (Grafana с `?kiosk`)

**Архитектурное решение:**
- PO переписка — per-user (thread_id = `po-user-{telegram_id}`), показываем на странице юзера
- Architect/Engineering/Deploy трейсы — per-project, показываем и на странице юзера и на странице проекта
- Langfuse iframe фильтруется через query params (если поддерживает) или показывается as-is на общей странице /tracing

## Steps

1. [ ] LLM Tracing page — Langfuse iframe
   - **Input**: `services/admin-frontend/src/pages/LogsPage.tsx` (паттерн), `src/App.tsx` (роуты), `src/components/layout/Sidebar.tsx`
   - **Output**: Новый файл `src/pages/TracingPage.tsx` — iframe на `/langfuse/?kiosk` (аналогично LogsPage). Роут `/tracing` в App.tsx. Sidebar пункт "LLM Tracing" enabled (убрать `disabled: true`).
   - **Test**: `npm run build` проходит. Навигация /tracing показывает Langfuse UI в iframe.

2. [ ] Users list page
   - **Input**: `services/admin-frontend/src/pages/ProjectsPage.tsx` (паттерн), API `GET /users/`
   - **Output**: Новый файл `src/pages/UsersPage.tsx` — таблица юзеров (telegram_id, username, first_name, is_admin, last_seen). Тип `User` в `src/types/api.ts`. Роут `/users` в App.tsx. Sidebar пункт "Users" (иконка Users из lucide-react) перед Projects.
   - **Test**: `npm run build` проходит. /users показывает список юзеров из API.

3. [ ] User detail page — info + projects
   - **Input**: `src/pages/ProjectDetailPage.tsx` (паттерн), API `GET /users/{id}`, `GET /projects/?owner_id={id}`
   - **Output**: Новый файл `src/pages/UserDetailPage.tsx`. Карточка юзера (telegram_id, username, name, is_admin, last_seen). Табы: Projects (список проектов юзера), Tracing (Langfuse iframe отфильтрованный по userId если возможно, иначе общий). Роут `/users/:id` в App.tsx.
   - **Test**: `npm run build` проходит. /users/:id показывает юзера и его проекты.

4. [ ] API: добавить фильтр owner_id в GET /projects/
   - **Input**: `services/api/src/routers/projects.py`, `services/api/src/schemas/project.py`
   - **Output**: Query param `owner_id` (optional int) в GET /projects/ — фильтрует проекты по owner_id. Нужен для User detail page чтобы показать проекты юзера.
   - **Test**: unit test — `GET /projects/?owner_id=1` возвращает только проекты этого юзера.

5. [ ] Project detail — добавить секцию LLM Tracing
   - **Input**: `src/pages/ProjectDetailPage.tsx`
   - **Output**: Новый таб "LLM Tracing" на странице проекта — iframe Langfuse (отфильтрованный по project name/tag если возможно, иначе общий со ссылкой). Добавить ссылку на Owner (юзера) в карточку проекта.
   - **Test**: `npm run build` проходит. Таб LLM Tracing виден на странице проекта.

6. [ ] Langfuse X-Frame-Options + CSP — убедиться что iframe работает
   - **Input**: `services/admin-frontend/nginx.conf`, langfuse docker-compose env vars
   - **Output**: Если Langfuse отдаёт `X-Frame-Options: DENY` или strict CSP — добавить `proxy_hide_header X-Frame-Options` и `proxy_hide_header Content-Security-Policy` в nginx location `/langfuse/` (аналогично тому как сделано для Grafana с `GF_SECURITY_ALLOW_EMBEDDING`). Проверить что iframe реально работает.
   - **Test**: Открыть /tracing — Langfuse UI рендерится в iframe без ошибок в консоли.

7. [ ] Unit tests для нового API фильтра
   - **Input**: `services/api/tests/unit/`
   - **Output**: Тест на `GET /projects/?owner_id=X` — фильтрация работает корректно. Тест что без owner_id возвращает все (для админов).
   - **Test**: `make test-api-unit` проходит.

