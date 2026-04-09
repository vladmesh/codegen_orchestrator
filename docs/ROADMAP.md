# Roadmap

> **Updated**: 2026-04-10
>
> Story-level milestones. Updated by `/close-sprint`. Individual tasks tracked in sprint directories.

# Product Roadmap

## Stabilize core pipeline

Довести генерацию Telegram-ботов до стабильного E2E. Юзер приходит в Telegram → общается с PO → получает работающего бота за 20-30 минут. Потом просит доработки через диалог → получает обновлённого бота. Включает: фикс известных багов, стабилизацию worker lifecycle, надёжный CI/CD pipeline.

- [x] Smart CI failure triage: worker reject signal + CI-fix task template
- [x] #51 SQLAlchemy JSON Mutation Tracking — Secrets Lost on Save
- [x] #50 Fix Description Loss in Create Flow
- [x] #48 Corrupted Checkpoint Recovery (orphan tool_calls)
- [x] #47 Race Condition in set_project_secret (parallel tool calls)
- [x] #42 Fix API Integration Test (test_post_projects_pure_db)
- [x] #8 Workspace Failure Counter
- [x] Project ID → UUID + schema cleanup
- [x] Fix compose.dev.yml ports conflict with orchestrator worker containers

- [x] #1004 CI gate: one push per story instead of per task
- [x] #1009 Worker local tests: add make lint + make test-unit to INSTRUCTIONS.md
- [x] #1010 STORY.md: generate .story/STORY.md with story goal, task list, references
- [x] Fix deploy failure classification and worker rejection pipeline
- [x] #52 Scaffold script не экранирует task_description
- [x] #21 Deploy Pre-Check
- [x] Observability stack: JSON logging + Loki + Grafana + correlation propagation
- [ ] Scaffolder: ensure-workspace gate — always verify workspace before pipeline proceeds
- [x] Unify workspace management: repo_id-based addressing, remove legacy workspace creation
- [x] #54 Deploy: inter-service URL должен использовать docker service name
- [ ] #60 Engineering worker work_item lifecycle (Step 5)
- [ ] Fix eager import chains in scaffolded projects

- [ ] Auto-generate routers from domain specs

- [ ] Add predefined module to existing project (make add-module)

- [ ] Unified handlers: error handling strategy

- [ ] Auto-update __init__.py re-exports after generation

- [ ] #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- [ ] Enum types in model field definitions

- [ ] Celery worker support

- [ ] #46 Rename duckduckgo_search → ddgs
- [ ] High-level architecture spec (connectivity graph)

- [ ] Spec-first observability (auto OpenTelemetry)

- [ ] Spec-only module storage (long-term)

- [x] Fix integration test compose (host venv shebang + readiness check)

- [x] Fix compose/deploy bugs (VAR:?, health check, .env.prod removal)

- [x] Fix generated code not included in Docker image

- [x] Add deptry for missing runtime dependency detection

- [x] Fix Makefile: add .env loading + make migrate target

- [x] Fix broken import in scaffolded user repository

- [x] Fix compose.dev.yml PATH + make setup idempotency

- [x] Add CreatedAtMixin (ORMBase forced updated_at on all models)

- [x] Fix tg_bot AGENTS.md wrong env var + add router/list examples

- [x] Add list_users operation to reference User domain

- [x] Spec-first async messaging (Redis Streams + FastStream)


## Create LessWrong random article bot — COMPLETE

_Создать Telegram бота, который показывает случайные статьи с LessWrong.com.

**Функциональность:**
1. При старте бота (/start) проверять, что пользователь — это администратор (ADMIN_TELEGRAM_ID) или был добавлен админом. Если нет — отклонять доступ с сообщением "У вас нет доступа к этому боту"
2. Главная функция: кнопка "Случайная статья" (или команда), которая показывает:
   - Заголовок случайной статьи с LessWrong.com
   - Ссылку на эту статью
3. Команда /add_user [telegram_id] — доступна только администратору, добавляет пользователя в список разрешённых. Сохранять список разрешённых пользователей в базе данных
4. Команда /list_users — показать список всех разрешённых пользователей (только для админа)

**Технические требования:**
- Использовать GraphQL API LessWrong (https://www.lesswrong.com/graphql) или публичный API для получения статей
- Если API недоступен, использовать парсинг главной страницы или RSS
- Хранить список разрешённых пользователей в PostgreSQL
- При каждом запросе проверять, есть ли user_id в списке разрешённых или равен ли он ADMIN_TELEGRAM_ID
- Бот должен быть приватным — только админ и добавленные пользователи

**Переменные окружения:**
- TELEGRAM_BOT_TOKEN — токен бота от BotFather
- ADMIN_TELEGRAM_ID — Telegram ID администратора (только он может добавлять пользователей и имеет доступ изначально)_

## Add article summary feature via LLM — COMPLETE

_Добавить функционал получения краткого саммари статьи через LLM.

**Изменения в интерфейсе:**
1. Переименовать существующую кнопку "Случайная статья" в "Новая случайная статья"
2. После того как статья показана пользователю, показывать две кнопки:
   - "Новая случайная статья" (получить другую случайную статью)
   - "Получить саммари" (получить краткое содержание текущей статьи)

**Функционал "Получить саммари":**
1. При нажатии на кнопку "Получить саммари":
   - Взять текст статьи с LessWrong (спарсить содержимое статьи по ссылке)
   - Отправить текст статьи в LLM через OpenRouter API
   - Попросить LLM создать краткое саммари на русском языке
   - Отправить саммари пользователю в тот же чат

2. Использовать OpenRouter API (https://openrouter.ai/docs):
   - API endpoint: https://openrouter.ai/api/v1/chat/completions
   - Использовать модель по умолчанию (например, gpt-3.5-turbo или любую доступную быструю модель)
   - Промпт примерно такой: "Вот статья с LessWrong. Создай краткое саммари на русском языке (3-5 абзацев), сохраняя ключевые идеи и аргументы: {текст статьи}"

**Технические детали:**
- Сохранять последнюю показанную статью в контексте пользователя (в базе или в памяти бота), чтобы знать, для какой статьи делать саммари
- При получении новой случайной статьи — обновлять этот контекст
- Показывать индикатор "печатает..." пока генерируется саммари
- Если статья слишком длинная для LLM, обрезать или разбить на части

**Переменные окружения:**
- OPENROUTER_API_KEY — API ключ для OpenRouter_

## Create fortune telling Telegram bot — COMPLETE

_Create a Telegram bot that provides fortune telling predictions with tarot cards, philosophy, and psychology elements.

CORE FLOW:

1. START MENU (/start command):
Show welcome message and inline keyboard with theme selection buttons:
- "Карьера и призвание"
- "Увлечения и развлечения"
- "Друзья и общение"
- "Романтика и любовь"
- "Комфорт и финансы"
- "Когнитивное развитие"
- "Ментальное развитие"
- "Новизна и неожиданности"
- "Случайная тема" (randomly picks one of the themes above)
- "Задать вопрос" (allows user to type their own question)

2. QUESTION INPUT:
If user selects "Задать вопрос", show instruction: "Напиши свой вопрос текстом, и я дам предсказание 🔮"
Wait for text message from user, then proceed to prediction generation.

3. PREDICTION GENERATION:
After receiving theme or question, generate and send prediction consisting of:

a) **Tarot card image**: randomly select 1 card from 78 Rider-Waite tarot deck and send its image. Use public domain Rider-Waite images (embed in project or use reliable public URLs).

b) **Philosophical quote**: generate a relevant philosophical quote using OpenRouter AI that fits the theme/question

c) **Psychological research**: generate a brief description (2-3 sentences) of a psychological study relevant to the theme/question using OpenRouter AI

d) **Beautiful inspirational quote**: generate an inspiring quote relevant to the theme/question using OpenRouter AI

e) **Tarot interpretation**: generate 2-3 sentences describing the theme/question through the lens of the drawn tarot card using OpenRouter AI

f) **Final prediction**: generate a positive and playful prediction based on all the above elements using OpenRouter AI

g) **LessWrong article link**: provide a link to a random article from lesswrong.com (maintain a list of ~20-30 popular LessWrong articles and pick randomly, or link to categories)

Format all elements nicely with emojis and formatting. Send as a single message or split logically.

4. REPEAT PROMPT:
After showing prediction, ask "Хочет ли задать ещё вопрос?" with inline buttons:
- "Да" → return to start menu
- "Нет" → send thank you message

AI GENERATION REQUIREMENTS:
- Use OpenRouter API (env: OPENROUTER_API_KEY)
- Create diverse prompts to ensure uniqueness (no repetitions more often than 1 in 100 times)
- Pass the selected tarot card name to AI prompts for accurate interpretation
- Use temperature ~0.8-0.9 for creative varied outputs
- Recommended model: anthropic/claude-3.5-sonnet or openai/gpt-4

DATABASE:
Store prediction history:
- user_id (bigint)
- theme_or_question (text) - selected theme or user's question text
- tarot_card (varchar) - name of drawn card
- created_at (timestamp)

TAROT CARDS:
Use all 78 Rider-Waite tarot cards:
- 22 Major Arcana (The Fool, The Magician, etc.)
- 56 Minor Arcana (14 cards × 4 suits: Wands, Cups, Swords, Pentacles)

Find and use public domain Rider-Waite images (they're widely available). Store locally or use stable public URLs.

LESSWRONG ARTICLES:
Maintain a list of interesting LessWrong articles (20-30 links) in code and randomly select one. Examples:
- https://www.lesswrong.com/posts/XvN2QQpKTuEzgkZHY/the-blueeyes-puzzle
- https://www.lesswrong.com/posts/34XxbRFe54FycoCDw/the-bottom-line
- etc.

Or use category links like https://www.lesswrong.com/tags/rationality

BOT PERSONALITY:
- Friendly, mystical, playful tone
- Use appropriate emojis (🔮✨🌟💫🃏)
- Keep predictions positive and fun
- All messages in Russian

ACCESS:
Public bot - anyone can use it (no access restrictions)._

## Fix tg_bot crash on startup — COMPLETE

_The tg_bot service crashes on startup because it imports a module that does not exist:

```python
from shared.generated.events import get_broker
```

The directory `shared/generated/` does not exist in the repo. Only `shared/shared/__init__.py` and `shared/shared/http_client.py` exist.

Fix: Remove the `get_broker()` import from `services/tg_bot/src/main.py` and remove the `post_init` and `post_shutdown` hooks that use it. The bot does not need Redis event bus integration — it communicates via Telegram API only.

Also remove the `from shared.generated.events import get_broker` line (line 23) and the `post_init`/`post_shutdown` async functions and their references in `build_application()`.

After the fix, the bot should start successfully and respond to /start with theme selection keyboard._

## Make LessWrong articles relevant to user's theme — COMPLETE

_Улучшить генерацию предсказаний: статьи с LessWrong должны быть релевантны теме/вопросу пользователя, а итоговый прогноз должен включать идеи из этих статей.

ТЕКУЩЕЕ ПОВЕДЕНИЕ:
- Бот выбирает случайную статью из заранее подготовленного списка LessWrong
- Итоговый прогноз генерируется на основе карты таро, философской цитаты и психологического исследования

ТРЕБУЕМЫЕ ИЗМЕНЕНИЯ:

1. **Релевантный выбор статьи LessWrong:**
   - Вместо случайного выбора из фиксированного списка, использовать AI для подбора статьи, релевантной теме/вопросу пользователя
   - Либо: подготовить расширенный список статей с тегами/категориями и выбирать по смыслу
   - Либо: генерировать через AI краткое описание подходящей статьи + ссылку на категорию LessWrong (например, https://www.lesswrong.com/tags/rationality, /tags/decision-theory и т.д.)
   - Приоритет: использовать конкретные статьи, а не только категории, где это возможно

2. **Интеграция идей из статьи в прогноз:**
   - Итоговый прогноз должен включать не только карту таро, философскую цитату и психологию, но и смыслы/идеи из выбранной статьи LessWrong
   - AI должен при генерации прогноза учитывать ключевые идеи статьи (название статьи и её основная тема)
   - Прогноз должен органично связывать все элементы: карту таро, цитаты, психологию И рационалистические идеи из LessWrong

ТЕХНИЧЕСКИЕ ДЕТАЛИ:
- Использовать OpenRouter API для подбора релевантной статьи и генерации прогноза
- Можно расширить список статей LessWrong в коде (добавить больше статей с разными темами)
- При генерации прогноза передавать в промпт: тему/вопрос пользователя, карту таро, цитаты, психологическое исследование И название + краткое описание статьи LessWrong
- Прогноз должен оставаться позитивным и забавным

ПРИМЕРЫ СТАТЕЙ ПО ТЕМАМ:
- Карьера/призвание: статьи про рациональное принятие решений, productivity
- Когнитивное развитие: статьи про cognitive biases, learning
- Романтика: статьи про game theory, social dynamics
- Финансы: статьи про decision theory, economics
- И т.д.

Все остальное в боте остается без изменений._

## Test: CI-check no-commit flow — COMPLETE

_Integration test for allow_no_commit feature_

## Fix tarot card images not displaying — COMPLETE

_Исправить проблему с отображением изображений карт таро — картинки не всегда показываются пользователям.

ПРОБЛЕМА:
Пользователи сообщают, что изображения карт таро не всегда отображаются при генерации предсказаний.

ВОЗМОЖНЫЕ ПРИЧИНЫ:
1. **Нестабильные внешние URL**: если используются публичные URL для изображений карт, они могут быть недоступны или иметь rate limits
2. **Неправильная отправка изображений**: возможно, проблема в коде отправки фото через Telegram API
3. **Отсутствие fallback**: нет резервного варианта при недоступности изображений

ТРЕБУЕМОЕ РЕШЕНИЕ:

**Приоритет 1 - Надёжное хранение изображений:**
- Скачать все 78 изображений карт Райдера-Уэйта и сохранить их локально в проекте (в папке типа `tg_bot/assets/tarot_cards/`)
- Использовать публичные домены изображения (Rider-Waite deck в публичном доступе)
- Отправлять изображения через `FSInputFile` (локальные файлы) вместо URL

**Приоритет 2 - Проверка и логирование:**
- Добавить логирование при отправке изображений (успех/ошибка)
- Проверить, что все 78 файлов изображений присутствуют при старте бота
- Если файл не найден — логировать ошибку и отправлять предсказание без картинки (но с названием карты текстом)

**Приоритет 3 - Fallback:**
- Если локальное изображение недоступно, отправлять предсказание с текстовым описанием карты вместо картинки
- Сообщение должно включать название карты и эмодзи 🃏

**Технические детали:**
- Использовать aiogram `FSInputFile` для отправки локальных файлов
- Структура файлов: `tg_bot/assets/tarot_cards/major_arcana/00_the_fool.jpg`, `minor_arcana/wands/ace_of_wands.jpg` и т.д.
- При старте бота проверять наличие всех 78 файлов
- В логах четко видеть: какая карта выбрана, отправлено ли изображение успешно

ИСТОЧНИКИ ИЗОБРАЖЕНИЙ:
Rider-Waite tarot deck в публичном домене, можно взять из:
- Wikimedia Commons
- Sacred Texts
- Другие открытые источники

ТЕСТИРОВАНИЕ:
После исправления протестировать генерацию предсказаний 10-15 раз подряд — все изображения должны отображаться стабильно._

## Smoke test reopen

## Create reverse message bot — COMPLETE

_Create a Telegram bot that reverses any text message character by character (from last character to first).

**Access control:**
- Bot starts with admin-only access (admin ID in ADMIN_TELEGRAM_ID env var)
- Admin can add users to whitelist using commands
- Store allowed user IDs in PostgreSQL database

**Commands:**
- /start - send welcome message explaining what the bot does
- /add_user <user_id> - add user to whitelist (admin only, accepts Telegram user ID as argument)
- /list_users - show all allowed users with their IDs (admin only)
- /remove_user <user_id> - remove user from whitelist (admin only)

**Main functionality:**
- When bot receives any text message from an allowed user (admin or whitelisted), reverse the message character-by-character and send it back
- Example: "Привет мир" → "рим тевирП"
- Example: "Hello world!" → "!dlrow olleH"
- If message is from non-whitelisted user, ignore or send "Access denied"

**Database:**
- Table for whitelisted users: user_id (bigint, primary key), added_at (timestamp), added_by (bigint - who added them)
- Admin (ADMIN_TELEGRAM_ID) should be automatically included in access checks even if not in database

Bot username: @vlad_test_bot_factory_bot
Token stored as TELEGRAM_BOT_TOKEN
Admin ID stored as ADMIN_TELEGRAM_ID_

## Add /revert command to reverse text — COMPLETE

_Добавить команду /revert в бота, которая переворачивает текст задом наперёд.

**Функционал:**
- Команда: `/revert <текст>`
- Принимает аргумент (слово или фразу) после команды
- Возвращает этот текст в обратном порядке (задом наперёд)

**Примеры использования:**
- `/revert hello` → `olleh`
- `/revert привет` → `тевирп`
- `/revert hello world` → `dlrow olleh`

**Технические детали:**
- Команда доступна всем авторизованным пользователям бота (админ + добавленные через /add_user)
- Если аргумент не передан, показать сообщение с примером использования: "Использование: /revert <текст>"
- Просто развернуть строку в обратном порядке и отправить результат_

## Create random cat photo bot — COMPLETE

_Создать Telegram бота, который отправляет случайные фотографии котов по кнопке.

**Функциональность:**
1. При старте бота (/start) проверять, что пользователь — это администратор (ADMIN_TELEGRAM_ID). Если нет — отклонять доступ с сообщением "У вас нет доступа к этому боту"
2. Главная функция: кнопка "Случайный кот 🐱" (или команда /cat), которая отправляет случайную фотографию кота
3. Каждое нажатие на кнопку — новая случайная фотка кота

**Технические требования:**
- Использовать бесплатные публичные API для получения фотографий котов:
  - Вариант 1: https://cataas.com/cat (возвращает случайное изображение напрямую)
  - Вариант 2: https://api.thecatapi.com/v1/images/search (возвращает JSON с URL изображения)
  - Или любой другой бесплатный API с фотками котов
- Отправлять фото пользователю через Telegram API
- Бот должен быть приватным — только админ (ADMIN_TELEGRAM_ID) может использовать

**Переменные окружения:**
- TELEGRAM_BOT_TOKEN — токен бота от BotFather
- ADMIN_TELEGRAM_ID — Telegram ID администратора (только он имеет доступ)_

## smoke test for TESTING status — COMPLETE

## Server & Application Health Monitoring

Implement infrastructure monitoring: node_exporter + cadvisor on prod servers, health_checker worker with HTTP polling, application health probes, drift detection, auto-incidents with Telegram alerts. Source: brainstorm server-health-monitoring.md (bs-69482380).

- [x] #1011 Provisioning: install node_exporter + cadvisor + UFW rules
- [x] #1012 Prometheus text format parser for node_exporter + cadvisor metrics
- [x] #1013 Extend Server model with health metrics + metrics history table
- [x] #1014 Implement health_checker worker (HTTP polling + auto-incidents + alerts)
- [x] #1015 Admin UI: extended server health dashboard with per-container view + charts
- [x] #1016 Admin UI: application health status and response times
- [x] #1019 HTTP health prober for deployed applications + SSL expiry check
- [ ] #1017 Container drift detection via cadvisor (orphans/ghosts in health_checker)
- [ ] #1018 Daily SSH job: filesystem drift check + docker prune

## Smoke test story

- [ ] Initialize project foundation and verify smoke test

## Product decomposition + Architect node

PO умеет принимать от юзера высокоуровневое описание и формулировать из него продуктовые stories. Architect нода берёт story + контекст проекта (спеки, кодбаза) и дробит на технические tasks с зависимостями. Юзер видит stories (продуктовый уровень), tasks абстрагированы. Юзер может влиять на stories через диалог с PO.

Текущий фокус: выстроить pipeline scaffold → architect → worker по спеке docs/PIPELINE_V2.md. Ключевой brainstorm: bs-d302b6a1 (Architect Context & Worker Knowledge).

- [x] #45 PO: Context-Aware Env Variables & Hints
- [x] #44 PO: DuckDuckGo Search Tool
- [x] #43 PO: Сократический диалог и формирование ТЗ
- [x] Create scaffolder microservice
- [x] Architect receives tree + specs, creates tasks for diff
- [x] Worker-manager mounts workspace volume by repo_id
- [ ] /architect skill — Story decomposition into Tasks
- [ ] #62 /brainstorm resume — продолжение обсуждения существующего драфта
- [ ] #59 PO work item tools (Step 4)

## Admin dashboard v1 — COMPLETE

_Простейший веб-UI для оператора. Read-only: список юзеров, их проекты, статусы воркеров, переписки юзер↔PO. Минимальный стек._

## Frontend generation

Модуль фронтенда в service-template. Оркестратор умеет генерить сайты (SPA/SSR) с бэкендом и базой. Тот же флоу: описание → готовый сайт с доменом. Большой эпик, будет дробиться на под-stories.

## Post-release testing

Post-release QA через предустановленный Claude Code на прод-серверах. После успешного деплоя: story → TESTING → SSH на сервер → Claude Code тестирует по описанию стори как реальный пользователь (curl, Telethon для ботов). Pass → completed. Fail → новая task → engineering → deploy → QA loop. Brainstorm: bs-eece61a8 (post-release-qa-mvp.md).

- [x] Add TESTING status to StoryStatus + API transition endpoint + QA queue contract

## GitHub integration

Юзер может подключить свой GitHub аккаунт. Видит репозиторий своего проекта, может форкнуть. Для продвинутых юзеров — возможность контрибьютить в свой проект.

- [x] Repository model + migration
- [ ] Integrate Repository into production flows (webhook, scheduler, worker)

## Dev process automation

Internal tooling: task management system, skills, brainstorms, docs generation, DB seeding.

- [x] #55 WorkItem Model + API + Backlog Migration (Step 0)
- [x] #63 Milestone model + ROADMAP generation
- [x] Rename WorkItem→Task, Task→Run
- [x] Replace Milestone with Story type field (product/technical)
- [x] #56 /next skill via API (Step 1)
- [x] #57 /implement work item events (Step 2)
- [x] #61 Brainstorm model in DB
- [x] Story model + API
- [x] #64 Implement skill: PR flow + in_ci status + need_e2e
- [x] make sync — генерация docs из БД (backlog, roadmap, status, recent plans/brainstorms)
- [x] Story: priority + blocked_by fields
- [x] Seed DB — stories, repositories, historical tasks
- [x] #58 Skills → API + Simplified Model
- [ ] Context packer for agents (make context service=backend)

- [ ] CLI wrappers (my-framework init/sync/update)


## Admin dashboard v2

Расширение админки: логи воркеров и микросервисов, детали работы нод, возможность вмешиваться (перезапустить воркер, отменить задачу, отправить сообщение юзеру от имени PO).

## Refactoring & code health

Code quality improvements: splitting large files, reducing complexity, cleanup.

- [x] #1023 Queue contracts: Optional story_id + action field in DeployMessage/QAMessage
- [x] #18 Split engineering_worker.py (1088 LOC)
- [ ] #19 Split github.py Client (986 LOC)
- [ ] Extract type mappings into language-agnostic config

- [ ] Audit scaffold templates for best practices

- [x] Rewrite copier tests

- [x] Fix codegen quality (cosmetic bugs + param types + optional schemas)

- [x] Fix Jinja whitespace in doc templates + add cache mounts to Dockerfiles


## Conversation summarization

Суммаризация переписки PO↔юзер для экономии токенов. Базовый контекст-менеджмент: длинные диалоги сжимаются, ключевые решения сохраняются. Наброски уже есть в проекте.

## Architect node: sub-story decomposition

Архитектор умеет определять что story слишком большая и дробить на под-stories перед генерацией tasks. Или возвращать PO с пометкой "нужно уточнить scope".

## User dashboard

ЛК для нетехнического фаундера — продуктовые метрики по его проектам. Юзеры (DAU/WAU, new, returning), активность (requests/day, top endpoints), здоровье (p95, error rate). Pipeline: Promtail (prod) → Loki → Aggregator → PostgreSQL → API → SPA. Auth через Telegram бота (one-time token → JWT). Разбивка per-application (backend, tg_bot). Brainstorm: docs/brainstorms/user-dashboard-lk.md

- [x] #1031 Promtail on prod servers + expose Loki
- [x] #1032 Add com.codegen.project_id label to deployed containers
- [x] #1033 Analytics aggregation: models, Loki client, scheduler job
- [x] #1034 ЛК API: auth (one-time token → JWT) + analytics endpoints
- [x] #1035 Telegram bot: dashboard button with one-time token
- [x] #1036 ЛК frontend SPA (project list + dashboard)

## Human-in-the-loop

Тарифная модель: базовая подписка (AI only) → дорогая (подключаются живые разработчики). Оркестратор как прокладка к студии/аутсорсу. Механизм эскалации задач от AI к человеку и обратно.

## Worker swarm

Параллельные воркеры, оптимизация скорости генерации. Умное распределение задач, переиспользование контейнеров. Есть brainstorm в проекте.

- [ ] #10 Worker Lifecycle (Pause/Unpause)
- [ ] #41 Parallel Server Provisioning

## Pre-release testing

Feature-стенды, полноценный CI перед деплоем. Preview environments для юзера ("посмотри перед релизом").

- [ ] #11 E2E Tests Completion
- [ ] Auto-fuzzing and contract testing (schemathesis)

- [x] Add E2E CI job for unified handlers (dual-transport pipeline)


## Security hardening

Аудит серваков с пользовательским кодом, шифрование секретов, изоляция контейнеров, rate limiting, abuse protection.

- [ ] #7 Security Audit: Deploy Cleanup
- [ ] #2 Agent Hierarchy & Incident Response
- [ ] #20 API Key & SSH Key Encryption
- [ ] Unified handlers: transactional outbox pattern


## Full RAG

Интеллектуальный поиск по проекту, документации, истории переписки. Агенты (PO, Architect, Workers) быстро находят релевантный контекст вместо того чтобы грузить всё в промпт.

## Admin dashboard v3

Мегадашборд: все метрики, все юзеры, полная observability. Графики, алерты, drill-down в любую сущность.

- [x] #1020 SystemConfig: model + API + ConfigStore + switch services to DB configs
- [ ] #1021 ConfigStore helper with TTL cache
- [x] #1024 Thin API endpoints for admin actions (7 endpoints)
- [ ] #1022 Switch services from hardcoded constants to ConfigStore
- [x] #1025 Admin UI: Settings page (config + prompt editor)
- [x] #1026 Admin UI: action buttons on entity pages

# Technical Initiatives

## Rust migration

Rewrite service-template codegen and generated services from Python to Rust. Axum + SeaORM for services, Tera for templates. Goal: stricter feedback loop for agent-driven development, faster builds, fewer runtime bugs.

- [ ] Make YAML specs fully language-agnostic

- [ ] Rust PoC: backend service on Axum

- [ ] Rust PoC: Telegram bot on teloxide

- [ ] Research Tera as Jinja2 replacement for codegen

- [ ] Add Rust service type to services.yml


## Unlinked Tasks

- [ ] Fix noqa suppressions that mask real complexity
- [ ] #1005 Standardize PYTHONPATH and import patterns across service-template services
- [ ] debug test
- [ ] Add TTL/cleanup for stale Redis queue messages
- [ ] API authorization: scope worker access, protect destructive endpoints
- [x] Refactor shared: eliminate orchestrator code from worker containers
- [ ] #1003 Integration test: scheduler-langgraph story worker lifecycle
- [ ] Allocate ports only for modules that need host exposure
